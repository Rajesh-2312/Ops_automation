"""The Comms Service HTTP surface — CLAUDE.md §8's single outbound queue.

Five things are worth pinning about `app/api/comms.py`, and all five are here:

* **§8, the diff is the review surface.** Drafting renders the template against
  the structured values and stores the diff between that baseline and the body.
  A message that is pure substitution stores `identical: true`.
* **R4, the ladder and nothing beside it.** Draft, amend and submit work;
  approve, reject and release all return 501 while §14 Q3 is open, and the body
  names the question. Release before approval is a 409.
* **R5, the commercials wall.** On a `BYPASSRLS` connection nothing in the
  database refuses an LDE Executive a payout message. The refusal is in the
  router, and it lands before the row is read for a listing and before the
  program is read for a draft.
* **§11, one transaction.** Every transition writes its audit row through
  `write_within()`, kept apart from `write()` by the recording writer below so
  "atomic with the transition" is asserted rather than hoped.
* **Release transmits nothing.** There is no provider to assert against, which is
  the point; `test_comms_lifecycle.py` asserts that structurally.

The session is faked the way `test_api_approvals.py` fakes it: it holds ORM
instances, answers `get()` by primary key and `execute()` by selected entity, and
does not evaluate WHERE clauses. It accepts writes, because these endpoints exist
to persist.
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

from app.api import comms
from app.core.audit import AuditEvent, AuditWriter, get_audit_writer
from app.core.security import Principal, get_principal
from app.db.models import College, CommsMessage, Program
from app.db.session import get_session
from app.domain.enums import ArtifactState, ArtifactType, Persona, ProgramType
from app.services.comms import CommsChannel, CommsRecipientKind

COLLEGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROGRAM_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
MESSAGE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
USER_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

NOW = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
URL = f"/comms/messages/{MESSAGE_ID}"

TEMPLATE = "Hello {{name}},\nAttendance for {{month}} is complete.\nRegards,\nOps"
VALUES = {"name": "Rao", "month": "July 2026"}
BASELINE = "Hello Rao,\nAttendance for July 2026 is complete.\nRegards,\nOps"

TRANSITION_ROUTES = ("submit", "approve", "reject", "release")
BLOCKED_ROUTES = ("approve", "reject", "release")


# --- the fake session ----------------------------------------------------------


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """An in-memory stand-in for `AsyncSession`, keyed by mapped class.

    WHERE clauses are not evaluated: a test loads exactly the rows its scenario
    needs. The listing endpoint's own filtering — the commercials wall applied per
    row — happens in the router and is therefore still under test.
    """

    def __init__(self, *rows: Any) -> None:
        self.rows = list(rows)
        self.queries: list[str] = []
        self.commits = 0

    def _of(self, model: type[Any]) -> list[Any]:
        return [row for row in self.rows if isinstance(row, model)]

    async def get(self, model: type[Any], pk: Any) -> Any:
        self.queries.append(model.__name__)
        return next((row for row in self._of(model) if row.id == pk), None)

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        self.queries.append(entity.__name__)
        return FakeResult(self._of(entity))

    def add(self, obj: Any) -> None:
        self.rows.append(obj)

    async def commit(self) -> None:
        self.commits += 1


class RecordingAudit(AuditWriter):
    """Keeps the two write paths apart, so the router's choice is assertable.

    `write_within()` is the one a lifecycle transition must use
    (`app/core/audit.py`: "if losing the audit row would leave a money or
    approval decision unattributable").
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.best_effort: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:  # pragma: no cover - must not be used
        self.best_effort.append(event)

    async def write_within(self, session: AsyncSession, event: AuditEvent) -> None:
        self.events.append(event)


# --- scenario ------------------------------------------------------------------


def row(
    state: ArtifactState = ArtifactState.DRAFT,
    *,
    is_commercial: bool = False,
    body: str = "Hello Rao,\nAttendance for July 2026 is complete. Thanks.\nRegards,\nOps",
    content_hash: str | None = None,
    row_id: uuid.UUID = MESSAGE_ID,
) -> CommsMessage:
    """One `comms_messages` row in the state a scenario needs.

    `content_hash` mirrors the database biconditional: frozen states carry one,
    unfrozen states do not. No test in this file invents a digest — nothing here
    can reach APPROVED, because §14 Q3 is open.
    """
    frozen = state in {ArtifactState.APPROVED, ArtifactState.RELEASED}
    return CommsMessage(
        id=row_id,
        program_id=PROGRAM_ID,
        channel=CommsChannel.EMAIL,
        recipient_kind=CommsRecipientKind.COLLEGE,
        recipient_ref="principal@malineni.edu",
        recipient_name="Principal",
        template_key="attendance.complete.v1",
        template_body=BASELINE,
        template_values=dict(VALUES),
        subject="July attendance",
        body=body,
        diff={"version": 1, "identical": False, "hunks": []},
        is_commercial=is_commercial,
        related_artifact_type=None,
        related_artifact_id=None,
        state=state,
        content_hash=content_hash,
        version=1,
        supersedes_id=None,
        superseded_at=None,
        created_by=USER_ID,
        created_at=NOW,
        submitted_by=USER_ID if state is not ArtifactState.DRAFT else None,
        submitted_at=NOW if state is not ArtifactState.DRAFT else None,
        approved_by=USER_ID if frozen else None,
        approved_at=NOW if frozen else None,
        released_by=USER_ID if state is ArtifactState.RELEASED else None,
        released_at=NOW if state is ArtifactState.RELEASED else None,
        notes=None,
        updated_at=NOW,
    )


def scenario(*extra: Any) -> FakeSession:
    return FakeSession(
        College(id=COLLEGE_ID, name="Malineni Lakshmaiah"),
        Program(id=PROGRAM_ID, college_id=COLLEGE_ID, type=ProgramType.BCAP, name="bCAP 2026"),
        *extra,
    )


def principal(persona: Persona, *, reach: bool = True) -> Principal:
    return Principal(
        user_id=USER_ID,
        persona=persona,
        college_ids=frozenset({COLLEGE_ID}) if reach else frozenset(),
    )


def draft_body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "program_id": str(PROGRAM_ID),
        "channel": CommsChannel.EMAIL.value,
        "recipient_kind": CommsRecipientKind.COLLEGE.value,
        "recipient_ref": "principal@malineni.edu",
        "template_key": "attendance.complete.v1",
        "template": TEMPLATE,
        "template_values": dict(VALUES),
        "subject": "July attendance",
        "body": BASELINE,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def audit() -> RecordingAudit:
    return RecordingAudit()


@pytest.fixture
def client(audit: RecordingAudit) -> Iterator[TestClient]:
    """A one-router app. `create_app()` is not used — it builds `Settings` from
    the environment, which would make this suite depend on a developer's `.env`.
    Registering this router in `main.py` is somebody else's change."""
    app = FastAPI()
    app.include_router(comms.router)
    app.dependency_overrides[get_audit_writer] = lambda: audit
    with TestClient(app) as test_client:
        yield test_client


def post(client: TestClient, session: FakeSession, caller: Principal, url: str, **body: Any) -> Any:
    client.app.dependency_overrides[get_session] = lambda: session
    client.app.dependency_overrides[get_principal] = lambda: caller
    return client.post(url, json=dict(body))


def get(client: TestClient, session: FakeSession, caller: Principal, url: str) -> Any:
    client.app.dependency_overrides[get_session] = lambda: session
    client.app.dependency_overrides[get_principal] = lambda: caller
    return client.get(url)


def _route_body(route: str) -> dict[str, Any]:
    return {"reason": "wrong tone"} if route == "reject" else {}


# =============================================================================
# §8 — channel, recipient, template, and the diff
# =============================================================================


def test_drafting_stores_the_baseline_and_the_diff(client) -> None:
    """The two-step: render the template against the structured values, then diff
    the baseline against the body. Everything the diff reports is drafter prose."""
    session = scenario()
    response = post(client, session, principal(Persona.MANAGER), "/comms/messages", **draft_body())

    assert response.status_code == 201
    payload = response.json()
    assert payload["state"] == ArtifactState.DRAFT.value
    assert payload["template_body"] == BASELINE
    assert payload["diff"]["identical"] is True  # body IS the baseline here
    assert payload["template_values"] == VALUES


def test_a_drafter_edit_shows_up_as_a_hunk(client) -> None:
    session = scenario()
    edited = BASELINE.replace("is complete.", "is complete — well done to your team.")
    response = post(
        client,
        session,
        principal(Persona.MANAGER),
        "/comms/messages",
        **draft_body(body=edited),
    )

    diff = response.json()["diff"]
    assert diff["identical"] is False
    assert diff["hunks"][0]["template"] == ["Attendance for July 2026 is complete."]


def test_an_unfilled_slot_is_refused_with_422_naming_the_slot(client) -> None:
    """The braces survive into what the recipient reads, and a blank reads as a
    fact nobody has."""
    session = scenario()
    response = post(
        client,
        session,
        principal(Persona.MANAGER),
        "/comms/messages",
        **draft_body(template_values={"name": "Rao"}),
    )

    assert response.status_code == 422
    assert "month" in response.json()["detail"]


def test_a_float_amount_is_refused_r7(client) -> None:
    """A rupee amount reaching a trainer as 15584.000000000001 is wrong in the
    one way nobody forgives, so it is refused rather than rounded."""
    session = scenario()
    response = post(
        client,
        session,
        principal(Persona.MANAGER),
        "/comms/messages",
        **draft_body(template="pay {{net}}", body="pay 15584", template_values={"net": 15584.5}),
    )

    assert response.status_code == 422
    assert "R7" in response.json()["detail"]


# =============================================================================
# R4 — the ladder, and §14 Q3 stopping it
# =============================================================================


def test_submitting_moves_a_draft_to_pending_approval(client, audit) -> None:
    session = scenario(row())
    response = post(client, session, principal(Persona.MANAGER), f"{URL}/submit")

    assert response.status_code == 200
    assert response.json()["state"] == ArtifactState.PENDING_APPROVAL.value
    assert response.json()["submitted_by"] == str(USER_ID)
    assert session.commits == 1


@pytest.mark.parametrize("route", BLOCKED_ROUTES)
def test_approve_reject_and_release_return_501_naming_the_open_question(client, route: str) -> None:
    """§14 Q3 — "Approval authority for college-facing comms: Manager or Senior
    Manager?" — is unanswered, so nobody may approve, reject or release.

    501 and not 403, mirroring `app/api/approvals.py`: the caller is not
    forbidden, the organisation has no answer, and a 403 would send a Senior
    Manager hunting for a permission that does not exist.
    """
    state = ArtifactState.APPROVED if route == "release" else ArtifactState.PENDING_APPROVAL
    session = scenario(row(state, content_hash="deadbeef" if route == "release" else None))
    response = post(
        client,
        session,
        principal(Persona.SENIOR_MANAGER),
        f"{URL}/{route}",
        **_route_body(route),
    )

    assert response.status_code == 501
    assert "§14 Q3" in response.json()["detail"]


def test_the_queue_fills_and_stops(client) -> None:
    """The designed end state of Phase 4: a message can be drafted and submitted,
    and then nothing happens until a human answers Q3. That is R4 working."""
    session = scenario(row())
    assert post(client, session, principal(Persona.MANAGER), f"{URL}/submit").status_code == 200
    session.rows[-1].state = ArtifactState.PENDING_APPROVAL
    assert (
        post(client, session, principal(Persona.SENIOR_MANAGER), f"{URL}/approve").status_code
        == 501
    )


def test_releasing_an_unsubmitted_draft_is_a_409_before_any_authority_check(client) -> None:
    """The grammar refuses first: DRAFT has no edge to RELEASED, so the caller is
    told what the legal moves are rather than told the org has not decided."""
    session = scenario(row())
    response = post(client, session, principal(Persona.SENIOR_MANAGER), f"{URL}/release")

    assert response.status_code == 409
    assert "RELEASED" in response.json()["detail"]


def test_amending_a_draft_recomputes_the_stored_diff(client) -> None:
    """A stored diff that no longer describes the stored body is a review surface
    that lies."""
    session = scenario(row(body=BASELINE))
    response = client_patch(client, session, principal(Persona.MANAGER), f"Hi there\n{BASELINE}")

    assert response.status_code == 200
    assert response.json()["diff"]["identical"] is False
    assert response.json()["version"] == 1


def client_patch(client: TestClient, session: FakeSession, caller: Principal, body: str) -> Any:
    client.app.dependency_overrides[get_session] = lambda: session
    client.app.dependency_overrides[get_principal] = lambda: caller
    return client.patch(URL, json={"body": body})


def test_amending_a_submitted_message_is_refused(client) -> None:
    """A message in front of an approver must not change underneath them."""
    session = scenario(row(ArtifactState.PENDING_APPROVAL))
    response = client_patch(client, session, principal(Persona.MANAGER), "sneaky rewrite")

    assert response.status_code == 409


# =============================================================================
# R5 — the commercials wall
# =============================================================================


@pytest.mark.parametrize("route", TRANSITION_ROUTES)
@pytest.mark.parametrize("persona", [Persona.LDE_EXECUTIVE, Persona.TRAINER, Persona.COLLEGE])
def test_a_persona_outside_the_wall_cannot_touch_a_payout_message(
    client, route: str, persona: Persona
) -> None:
    """§4: an LDE Executive has NO commercials, and "your July invoice of ₹14,035
    is approved" is the payout restated in prose."""
    session = scenario(row(ArtifactState.PENDING_APPROVAL, is_commercial=True))
    response = post(client, session, principal(persona), f"{URL}/{route}", **_route_body(route))

    assert response.status_code == 403


def test_an_lde_executive_cannot_read_a_payout_message(client) -> None:
    session = scenario(row(is_commercial=True))
    assert get(client, session, principal(Persona.LDE_EXECUTIVE), URL).status_code == 403


def test_an_lde_executive_does_see_an_operational_message(client) -> None:
    """The wall is a property of the ROW, not of the table — 1700's two policies,
    and this is the half that must still return rows."""
    session = scenario(row(is_commercial=False))
    assert get(client, session, principal(Persona.LDE_EXECUTIVE), URL).status_code == 200


def test_the_queue_listing_filters_commercial_rows_rather_than_refusing(client) -> None:
    """A Manager and an LDE Executive both legitimately read this queue and should
    see different rows in it, which is exactly what the two SQL policies do."""
    session = scenario(
        row(is_commercial=False),
        row(is_commercial=True, row_id=uuid.uuid4()),
    )
    url = f"/comms/messages?program_id={PROGRAM_ID}"

    manager = get(client, session, principal(Persona.MANAGER), url).json()
    lde = get(client, session, principal(Persona.LDE_EXECUTIVE), url).json()

    assert len(manager["messages"]) == 2
    assert len(lde["messages"]) == 1
    assert lde["messages"][0]["is_commercial"] is False


def test_a_manager_without_reach_is_refused(client) -> None:
    """The wall and the scope are separate conjuncts (1700, 1300, 0700). A
    Manager clears the wall and still may not act outside their colleges."""
    session = scenario(row(is_commercial=True))
    response = post(client, session, principal(Persona.MANAGER, reach=False), f"{URL}/submit")

    assert response.status_code == 403


def test_drafting_a_remuneration_message_forces_the_commercial_flag(client) -> None:
    """R5 forced rather than trusted: the SQL CHECK is a backstop, not the only
    guard. A drafter that forgets the flag does not get an unwalled row."""
    session = scenario()
    response = post(
        client,
        session,
        principal(Persona.MANAGER),
        "/comms/messages",
        **draft_body(
            is_commercial=False,
            related_artifact_type=ArtifactType.REMUNERATION_SHEET.value,
            related_artifact_id=str(uuid.uuid4()),
        ),
    )

    assert response.status_code == 201
    assert response.json()["is_commercial"] is True


def test_an_lde_executive_cannot_draft_a_commercial_message_at_all(client) -> None:
    session = scenario()
    response = post(
        client,
        session,
        principal(Persona.LDE_EXECUTIVE),
        "/comms/messages",
        **draft_body(is_commercial=True),
    )

    assert response.status_code == 403


# =============================================================================
# §11 — the audit trail
# =============================================================================


def test_every_transition_writes_its_audit_row_atomically(client, audit) -> None:
    """`write_within()`, not `write()`: the state change and the evidence commit
    together or neither does."""
    session = scenario(row())
    post(client, session, principal(Persona.MANAGER), f"{URL}/submit")

    assert len(audit.events) == 1
    assert audit.best_effort == []
    assert audit.events[0].entity_table == "comms_messages"
    assert audit.events[0].actor_id == USER_ID


def test_drafting_is_audited_too_because_it_is_how_the_queue_fills(client, audit) -> None:
    session = scenario()
    post(client, session, principal(Persona.MANAGER), "/comms/messages", **draft_body())

    assert [event.action for event in audit.events] == ["comms.queued"]
    assert audit.events[0].before is None


def test_a_refused_transition_writes_nothing(client, audit) -> None:
    """A 501 is not an event. Nothing happened, and the trail must not suggest
    otherwise."""
    session = scenario(row(ArtifactState.PENDING_APPROVAL))
    post(client, session, principal(Persona.SENIOR_MANAGER), f"{URL}/approve")

    assert audit.events == []
    assert session.commits == 0


def test_a_pure_read_writes_no_audit_row(client, audit) -> None:
    session = scenario(row())
    get(client, session, principal(Persona.MANAGER), URL)

    assert audit.events == []


# =============================================================================
# R4 — superseding
# =============================================================================


def test_superseding_a_draft_is_refused_because_that_is_an_amendment(client) -> None:
    session = scenario(row())
    client.app.dependency_overrides[get_session] = lambda: session
    client.app.dependency_overrides[get_principal] = lambda: principal(Persona.MANAGER)
    response = client.post(f"{URL}/supersede", json={"body": "rewritten"})

    assert response.status_code == 409
    assert "amend_message" in response.json()["detail"]


def test_supersede_demands_values_when_a_new_template_is_supplied(client) -> None:
    """A baseline rendered from stale values would make the successor's diff
    describe facts nobody re-fetched (R1)."""
    session = scenario(row(ArtifactState.APPROVED, content_hash="deadbeef"))
    client.app.dependency_overrides[get_session] = lambda: session
    client.app.dependency_overrides[get_principal] = lambda: principal(Persona.MANAGER)
    response = client.post(f"{URL}/supersede", json={"body": "x", "template": "hi {{name}}"})

    assert response.status_code == 422
    assert "template_values" in response.json()["detail"]


def test_superseding_an_approved_message_opens_version_two_and_stamps_the_first(
    client, audit
) -> None:
    session = scenario(row(ArtifactState.APPROVED, content_hash="deadbeef"))
    client.app.dependency_overrides[get_session] = lambda: session
    client.app.dependency_overrides[get_principal] = lambda: principal(Persona.MANAGER)
    response = client.post(f"{URL}/supersede", json={"body": "Rewritten entirely"})

    assert response.status_code == 201
    payload = response.json()
    assert payload["version"] == 2
    assert payload["state"] == ArtifactState.DRAFT.value
    assert payload["supersedes_id"] == str(MESSAGE_ID)
    assert payload["content_hash"] is None
    # the predecessor is left exactly as approved, and marked
    predecessor = next(
        r for r in session.rows if isinstance(r, CommsMessage) and r.id == MESSAGE_ID
    )
    assert predecessor.state is ArtifactState.APPROVED
    assert predecessor.superseded_at is not None
    assert len(audit.events) == 1


# =============================================================================
# R3 — no agent path
# =============================================================================


def test_every_route_requires_an_authenticated_principal() -> None:
    """R3: "Release endpoints require an authenticated human session." Asserted
    over the router's own dependencies rather than by calling, so a route added
    later without the dependency fails here."""
    for route in comms.router.routes:
        dependant = route.dependant  # type: ignore[attr-defined]
        names = {sub.call.__name__ for sub in dependant.dependencies if sub.call is not None}
        assert "get_principal" in names, route.path  # type: ignore[attr-defined]
