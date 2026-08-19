"""The approval HTTP surface — R4's lifecycle, R5's wall, and §14 Q3's gap.

Five things are worth pinning about `app/api/approvals.py`, and all five are
here:

* **R4, approval and release are separate.** Release before approval is refused;
  approve-then-release works; and each act writes its OWN audit row through
  `write_within()`, which the recording writer below keeps apart from `write()`
  so "the audit row is atomic with the transition" is asserted rather than hoped.
* **R4, approval freezes.** Editing the source row after approval makes the
  payload recomputed at release disagree with the stored `content_hash`, and the
  release is refused with 409.
* **R5, the commercials wall.** On a `BYPASSRLS` connection nothing in the
  database refuses an LDE Executive a payout's approval trail. The refusal is in
  the router and lands before any query, so the wall tests assert zero queries.
* **§4 authority.** A Manager may READ a payout and may not APPROVE one —
  `APPROVAL_AUTHORITY` gives remuneration sheets to the Senior Manager alone.
* **§14 Q3.** An artifact type with no defined authority is refused with 501 and
  a message naming the open question, not with a permissive default.

The session is faked the way `test_api_payouts.py` fakes it: it holds ORM
instances, answers `get()` by primary key and `execute()` by selected entity, and
does not evaluate WHERE clauses. It DOES accept writes, because unlike the payout
router these endpoints exist to persist a transition.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import approvals
from app.core.audit import AuditEvent, AuditWriter, get_audit_writer
from app.core.security import Principal, get_principal
from app.db.models import (
    ArtifactVersion,
    College,
    GovernanceReport,
    Program,
    ProgramDocument,
    RemunerationSheet,
)
from app.db.session import get_session
from app.domain.enums import ArtifactState, ArtifactType, DocStatus, Persona, ProgramType

# --- identity ----------------------------------------------------------------

COLLEGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROGRAM_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
SHEET_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
REPORT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
DOCUMENT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
TRAINER_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
VERSION_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
USER_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")

NOW = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

SHEET_URL = f"/approvals/{ArtifactType.REMUNERATION_SHEET.value}/{SHEET_ID}"
REPORT_URL = f"/approvals/{ArtifactType.GOVERNANCE_REPORT.value}/{REPORT_ID}"
DOCUMENT_URL = f"/approvals/{ArtifactType.PROGRAM_DOCUMENT.value}/{DOCUMENT_ID}"

WRITE_ROUTES = ("submit", "approve", "reject", "release")


# --- the fake session ---------------------------------------------------------


class FakeResult:
    """Just enough of `sqlalchemy.Result` for the call shapes this router uses."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """An in-memory stand-in for `AsyncSession`, keyed by mapped class.

    WHERE clauses are not evaluated — a test loads exactly the rows its scenario
    has — with one exception that matters: `_current_version()` filters on
    `superseded_at is null`, and a version history test needs a superseded row to
    be excluded from it. So `execute()` applies that one filter itself, which is
    the only predicate this router's correctness depends on.
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
        rows = self._of(entity)
        if entity is ArtifactVersion and "superseded_at IS NULL" in str(statement):
            rows = [row for row in rows if row.superseded_at is None]
        return FakeResult(rows)

    def add(self, obj: Any) -> None:
        self.rows.append(obj)

    async def commit(self) -> None:
        self.commits += 1


class RecordingAudit(AuditWriter):
    """Keeps what it was given, and keeps the two write paths apart.

    `write_within()` is the one an approval must use (`app/core/audit.py`: "if
    losing the audit row would leave a money or approval decision
    unattributable"). Recording them separately is what lets a test assert the
    router chose it rather than the best-effort `write()`.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.best_effort: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:  # pragma: no cover - must not be used
        self.best_effort.append(event)

    async def write_within(self, session: AsyncSession, event: AuditEvent) -> None:
        self.events.append(event)


# --- scenario -----------------------------------------------------------------


def _sheet(net: str = "14035") -> RemunerationSheet:
    """CLAUDE.md §6 fixture one, as a persisted row: VEMA PRUDHVI SAI, bCAP."""
    return RemunerationSheet(
        id=SHEET_ID,
        trainer_id=TRAINER_ID,
        program_id=PROGRAM_ID,
        work_order_id=None,
        period_start=dt.date(2026, 7, 26),
        period_end=dt.date(2026, 7, 31),
        rate=Decimal("80000"),
        payable_days=Decimal("6"),
        days_in_month=31,
        earned=Decimal("15484"),
        ta_da=Decimal("100"),
        gross=Decimal("15584"),
        tds_rate=Decimal("0.10"),
        tds=Decimal("1548"),
        net_amount=Decimal(net),
        amount_in_words="Fourteen Thousand Thirty Five Rupees Only",
        currency="INR",
        payout_status=DocStatus.NOT_STARTED,
        invoice_no="VEMA/26-27/JUL1",
        invoice_pan="VEMAP1234K",
        created_at=NOW,
        updated_at=NOW,
    )


def version(
    state: ArtifactState = ArtifactState.DRAFT,
    *,
    artifact_type: ArtifactType = ArtifactType.REMUNERATION_SHEET,
    artifact_id: uuid.UUID = SHEET_ID,
    number: int = 1,
    content_hash: str | None = None,
    superseded: bool = False,
    row_id: uuid.UUID = VERSION_ID,
) -> ArtifactVersion:
    """One `artifact_versions` row in the state the scenario needs.

    `content_hash` mirrors the database's biconditional CHECK: frozen states
    carry one, unfrozen states do not. The helper does not compute it — the
    approve-then-release tests take the real digest off the approve response, so
    a hash in this file is never one the test invented.
    """
    frozen = state in {ArtifactState.APPROVED, ArtifactState.RELEASED}
    return ArtifactVersion(
        id=row_id,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        version=number,
        state=state,
        content_hash=content_hash,
        created_by=USER_ID,
        created_at=NOW,
        submitted_by=USER_ID if state is not ArtifactState.DRAFT else None,
        submitted_at=NOW if state is not ArtifactState.DRAFT else None,
        approved_by=USER_ID if frozen else None,
        approved_at=NOW if frozen else None,
        released_by=USER_ID if state is ArtifactState.RELEASED else None,
        released_at=NOW if state is ArtifactState.RELEASED else None,
        superseded_at=NOW if superseded else None,
        updated_at=NOW,
    )


def scenario(*extra: Any, sheet: RemunerationSheet | None = None) -> FakeSession:
    """One college, one program, and one row of each artifact type."""
    return FakeSession(
        College(id=COLLEGE_ID, name="Malineni Lakshmaiah"),
        Program(id=PROGRAM_ID, college_id=COLLEGE_ID, type=ProgramType.BCAP, name="bCAP 2026"),
        sheet if sheet is not None else _sheet(),
        GovernanceReport(
            id=REPORT_ID,
            program_id=PROGRAM_ID,
            title="July governance",
            url="https://example.invalid/report",
            created_at=NOW,
            updated_at=NOW,
        ),
        ProgramDocument(
            id=DOCUMENT_ID,
            program_id=PROGRAM_ID,
            document_template_id=None,
            category="mou",
            name="MoU",
            status="not_started",
            created_at=NOW,
            updated_at=NOW,
        ),
        *extra,
    )


def principal(persona: Persona, *, reach: bool = True) -> Principal:
    return Principal(
        user_id=USER_ID,
        persona=persona,
        college_ids=frozenset({COLLEGE_ID}) if reach else frozenset(),
    )


@pytest.fixture
def audit() -> RecordingAudit:
    return RecordingAudit()


@pytest.fixture
def client(audit: RecordingAudit) -> Iterator[TestClient]:
    """A one-router app.

    `app.main.create_app()` is not used: it builds `Settings` from the
    environment, which would make this suite depend on a developer's `.env`.
    Registration in `main.py` has its own test at the bottom of the file.
    """
    app = FastAPI()
    app.include_router(approvals.router)
    app.dependency_overrides[get_audit_writer] = lambda: audit
    with TestClient(app) as test_client:
        yield test_client


def call(
    client: TestClient,
    session: FakeSession,
    caller: Principal,
    url: str,
    **body: Any,
) -> Any:
    """POST `url` as `caller`, against `session`."""
    client.app.dependency_overrides[get_session] = lambda: session
    client.app.dependency_overrides[get_principal] = lambda: caller
    return client.post(url, json=dict(body))


def _body(route: str) -> dict[str, Any]:
    """The minimum valid body for a route. Only `reject` requires one."""
    return {"reason": "not right"} if route == "reject" else {}


def read(client: TestClient, session: FakeSession, caller: Principal, url: str) -> Any:
    client.app.dependency_overrides[get_session] = lambda: session
    client.app.dependency_overrides[get_principal] = lambda: caller
    return client.get(url)


# =============================================================================
# R5 — the commercials wall
# =============================================================================


@pytest.mark.parametrize("route", WRITE_ROUTES)
@pytest.mark.parametrize("persona", [Persona.LDE_EXECUTIVE, Persona.TRAINER, Persona.COLLEGE])
def test_a_persona_outside_the_wall_gets_nothing(client, route, persona) -> None:
    """§4: an LDE Executive has NO commercials, and a payout is a commercial.

    "Zero rows" is asserted literally. The refusal lands before a single query,
    so the endpoint cannot even confirm the sheet exists — on a `BYPASSRLS`
    connection that ordering is the whole protection.
    """
    session = scenario(version(ArtifactState.PENDING_APPROVAL))
    response = call(client, session, principal(persona), f"{SHEET_URL}/{route}", **_body(route))

    assert response.status_code == 403
    assert session.queries == []


def test_an_lde_executive_cannot_read_a_payouts_approval_trail(client) -> None:
    """The history is walled too: it carries the approver's name and the period."""
    session = scenario(version(ArtifactState.APPROVED, content_hash="deadbeef"))
    response = read(client, session, principal(Persona.LDE_EXECUTIVE), f"{SHEET_URL}/versions")

    assert response.status_code == 403
    assert session.queries == []


def test_a_manager_without_reach_is_refused(client) -> None:
    """The wall and the scope are separate conjuncts (1300, 0700).

    A Manager clears `can_see_commercials()` and still may not act on a payout in
    a college they are not assigned to.
    """
    session = scenario(version())
    response = call(client, session, principal(Persona.MANAGER, reach=False), f"{SHEET_URL}/submit")

    assert response.status_code == 403
    assert "college" in response.json()["detail"].lower()


def test_an_lde_executive_may_reach_a_non_commercial_artifact(client) -> None:
    """1300's second policy: `is_internal() and not artifact_is_commercial()`.

    A governance report is not a commercial table (0700 is explicit), so an LDE
    Executive passes the wall — and then hits §14 Q3, which is the next test.
    """
    session = scenario(version(artifact_type=ArtifactType.GOVERNANCE_REPORT, artifact_id=REPORT_ID))
    response = read(client, session, principal(Persona.LDE_EXECUTIVE), f"{REPORT_URL}/versions")

    assert response.status_code == 200


def test_a_commercial_program_document_is_walled_by_category(client) -> None:
    """`artifact_is_commercial()` splits program_documents by CATEGORY, as 1000 does."""
    session = scenario(
        ProgramDocument(
            id=DOCUMENT_ID,
            program_id=PROGRAM_ID,
            document_template_id=None,
            category="remuneration",
            name="Remuneration sheet",
            status="not_started",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.rows = [row for row in session.rows if getattr(row, "category", None) != "mou"]
    response = read(client, session, principal(Persona.LDE_EXECUTIVE), f"{DOCUMENT_URL}/versions")

    assert response.status_code == 403


# =============================================================================
# §4 — approval authority
# =============================================================================


def test_a_manager_may_not_approve_a_remuneration_sheet(client, audit) -> None:
    """§4 puts "payout approval" in the Senior Manager's column, and only there.

    The Manager passes the wall — they may read this payout — and is refused at
    the authority check. Nothing is written and no audit row is raised.
    """
    session = scenario(version(ArtifactState.PENDING_APPROVAL))
    response = call(client, session, principal(Persona.MANAGER), f"{SHEET_URL}/approve")

    assert response.status_code == 403
    assert "senior_manager" in response.json()["detail"]
    assert session.commits == 0
    assert audit.events == []


def test_a_manager_may_not_reject_either(client) -> None:
    """The power to withhold approval is the power to approve."""
    session = scenario(version(ArtifactState.PENDING_APPROVAL))
    response = call(
        client, session, principal(Persona.MANAGER), f"{SHEET_URL}/reject", reason="wrong rate"
    )

    assert response.status_code == 403


def test_a_manager_may_still_submit(client) -> None:
    """Submission needs no authority — §8 level 2 is "propose, human edits"."""
    session = scenario(version())
    response = call(client, session, principal(Persona.MANAGER), f"{SHEET_URL}/submit")

    assert response.status_code == 200
    assert response.json()["state"] == ArtifactState.PENDING_APPROVAL.value


# =============================================================================
# §14 Q3 — an unanswered governance question, surfaced as such
# =============================================================================


@pytest.mark.parametrize("route", ["approve", "reject"])
def test_an_artifact_type_with_no_defined_authority_is_refused(client, route) -> None:
    """`APPROVAL_AUTHORITY` has no entry for governance reports, on purpose.

    501, not 403 and not a permissive default: the caller is not forbidden, the
    organisation has not decided. The message must name the open question so the
    person reading it learns what has to happen, and it must NOT be fixable by
    editing `app/domain/enums.py`.
    """
    session = scenario(
        version(
            ArtifactState.PENDING_APPROVAL,
            artifact_type=ArtifactType.GOVERNANCE_REPORT,
            artifact_id=REPORT_ID,
        )
    )
    response = call(
        client,
        session,
        principal(Persona.SENIOR_MANAGER),
        f"{REPORT_URL}/{route}",
        **_body(route),
    )

    detail = response.json()["detail"]
    assert response.status_code == 501
    assert "governance_reports" in detail
    assert "Q3" in detail
    assert session.commits == 0


def test_release_is_refused_for_an_undefined_authority_too(client) -> None:
    """Release authority is read as approval authority (`state_machine` docstring).

    Reaching this needs an APPROVED report version — which cannot be produced
    through this API at all, since approving one is the 501 above. It is
    constructed directly here precisely to prove the refusal is not an accident
    of the transition order: even an already-approved report cannot be released
    while Q3 is open.
    """
    session = scenario(
        version(
            ArtifactState.APPROVED,
            artifact_type=ArtifactType.GOVERNANCE_REPORT,
            artifact_id=REPORT_ID,
            content_hash="a" * 64,
        )
    )
    response = call(client, session, principal(Persona.SENIOR_MANAGER), f"{REPORT_URL}/release")

    assert response.status_code == 501
    assert "Q3" in response.json()["detail"]


def test_the_undefined_authority_does_not_block_submission(client) -> None:
    """A report can still be prepared and queued. It just cannot be signed off."""
    session = scenario(version(artifact_type=ArtifactType.GOVERNANCE_REPORT, artifact_id=REPORT_ID))
    response = call(client, session, principal(Persona.MANAGER), f"{REPORT_URL}/submit")

    assert response.status_code == 200


# =============================================================================
# R4 — approve and release are separate acts
# =============================================================================


def test_release_before_approval_is_refused(client, audit) -> None:
    """`ALLOWED_TRANSITIONS` has no PENDING_APPROVAL -> RELEASED edge."""
    session = scenario(version(ArtifactState.PENDING_APPROVAL))
    response = call(client, session, principal(Persona.SENIOR_MANAGER), f"{SHEET_URL}/release")

    assert response.status_code == 409
    assert "PENDING_APPROVAL -> RELEASED" in response.json()["detail"]
    assert session.commits == 0
    assert audit.events == []


def test_release_from_draft_is_refused(client) -> None:
    session = scenario(version())
    response = call(client, session, principal(Persona.SENIOR_MANAGER), f"{SHEET_URL}/release")

    assert response.status_code == 409


def test_approve_then_release_works_and_writes_two_audit_rows(client, audit) -> None:
    """R4's central claim, end to end.

    Approval freezes and hashes and stops there — the response is APPROVED with a
    `released_at` of None, which is the assertion that approval did not release.
    Release is a second request, and the two produce two distinct audit rows
    written through `write_within()` so each is atomic with its own transition.
    """
    row = version(ArtifactState.PENDING_APPROVAL)
    session = scenario(row)
    approver = principal(Persona.SENIOR_MANAGER)

    approved = call(client, session, approver, f"{SHEET_URL}/approve").json()
    assert approved["state"] == ArtifactState.APPROVED.value
    assert approved["content_hash"]
    assert approved["approved_by"] == str(USER_ID)
    assert approved["released_at"] is None
    assert approved["released_by"] is None

    released = call(client, session, approver, f"{SHEET_URL}/release").json()
    assert released["state"] == ArtifactState.RELEASED.value
    assert released["released_by"] == str(USER_ID)
    # The freeze survives release: same version, same digest.
    assert released["content_hash"] == approved["content_hash"]
    assert released["version"] == approved["version"]

    assert [event.action for event in audit.events] == [
        "artifact.approved",
        "artifact.released",
    ]
    assert audit.best_effort == [], "an approval must use write_within(), not write()"
    assert session.commits == 2


def test_a_released_artifact_cannot_be_released_again(client) -> None:
    """RELEASED is terminal (R4)."""
    session = scenario(version(ArtifactState.RELEASED, content_hash="x" * 64))
    response = call(client, session, principal(Persona.SENIOR_MANAGER), f"{SHEET_URL}/release")

    assert response.status_code == 409


def test_no_route_approves_and_releases_in_one_call(client) -> None:
    """Structural, so it fails the moment somebody adds the convenient shortcut.

    R4: "Approval and release are separate actions with separate audit rows."
    """
    paths = {route.path for route in approvals.router.routes}  # type: ignore[attr-defined]
    assert not [
        path for path in paths if "approve" in path and ("release" in path or "publish" in path)
    ]


# =============================================================================
# R4 — approval freezes, and the freeze is re-verified at release
# =============================================================================


def test_a_payload_edited_after_approval_cannot_be_released(client, audit) -> None:
    """The reason `verify_frozen()` runs at release rather than only at approval.

    The sheet is approved, then its net pay is edited in the system of record —
    which R4 forbids; an edit makes a NEW version. The payload recomputed at
    release no longer hashes to the frozen digest, and the release is refused.
    Without this recheck the edit would leave the system on the approver's
    signature.
    """
    sheet = _sheet()
    session = scenario(version(ArtifactState.PENDING_APPROVAL), sheet=sheet)
    approver = principal(Persona.SENIOR_MANAGER)

    assert call(client, session, approver, f"{SHEET_URL}/approve").status_code == 200
    audit.events.clear()

    sheet.net_amount = Decimal("99999")  # the tamper
    response = call(client, session, approver, f"{SHEET_URL}/release")

    assert response.status_code == 409
    assert "edited after approval" in response.json()["detail"]
    assert audit.events == []
    assert session.commits == 1, "the refused release must not commit"


def test_an_untouched_payload_still_releases(client) -> None:
    """The control for the test above: same path, nothing edited."""
    session = scenario(version(ArtifactState.PENDING_APPROVAL))
    approver = principal(Persona.SENIOR_MANAGER)

    assert call(client, session, approver, f"{SHEET_URL}/approve").status_code == 200
    assert call(client, session, approver, f"{SHEET_URL}/release").status_code == 200


# =============================================================================
# Rejection needs a reason
# =============================================================================


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_rejection_without_a_stated_reason_is_refused(client, reason) -> None:
    """A blank reason is how a required field becomes a formality."""
    session = scenario(version(ArtifactState.PENDING_APPROVAL))
    response = call(
        client, session, principal(Persona.SENIOR_MANAGER), f"{SHEET_URL}/reject", reason=reason
    )

    assert response.status_code == 422
    assert session.commits == 0


def test_a_rejection_returns_the_artifact_to_draft_with_its_reason(client, audit) -> None:
    session = scenario(version(ArtifactState.PENDING_APPROVAL))
    response = call(
        client,
        session,
        principal(Persona.SENIOR_MANAGER),
        f"{SHEET_URL}/reject",
        reason="rate disagrees with the signed work order",
    )
    body = response.json()

    assert response.status_code == 200
    assert body["state"] == ArtifactState.DRAFT.value
    assert body["content_hash"] is None
    assert body["version"] == 1, "a rejected draft is reworked, not superseded"
    assert body["notes"] == "rate disagrees with the signed work order"
    assert audit.events[-1].action == "artifact.rejected"


# =============================================================================
# Submission
# =============================================================================


def test_submitting_an_artifact_with_no_version_row_opens_version_one(client) -> None:
    """Nothing else creates a version row, and a lifecycle nobody can enter is not one."""
    session = scenario()
    response = call(client, session, principal(Persona.SENIOR_MANAGER), f"{SHEET_URL}/submit")
    body = response.json()

    assert response.status_code == 200
    assert body["version"] == 1
    assert body["state"] == ArtifactState.PENDING_APPROVAL.value
    assert body["content_hash"] is None, "nothing is frozen before approval (R4)"
    assert body["submitted_by"] == str(USER_ID)


def test_submitting_twice_is_refused(client) -> None:
    session = scenario(version(ArtifactState.PENDING_APPROVAL))
    response = call(client, session, principal(Persona.SENIOR_MANAGER), f"{SHEET_URL}/submit")

    assert response.status_code == 409


def test_a_missing_artifact_is_a_404_for_a_caller_who_cleared_the_wall(client) -> None:
    """404 leaks membership, so it is only acceptable after persona and wall pass."""
    session = scenario()
    url = f"/approvals/{ArtifactType.REMUNERATION_SHEET.value}/{uuid.uuid4()}/submit"
    response = call(client, session, principal(Persona.SENIOR_MANAGER), url)

    assert response.status_code == 404


# =============================================================================
# Version history
# =============================================================================


def test_the_history_lists_every_version_oldest_first(client) -> None:
    """Including superseded ones — the approved version is the record of a decision."""
    session = scenario(
        version(ArtifactState.APPROVED, content_hash="a" * 64, number=1, superseded=True),
        version(number=2, row_id=uuid.uuid4()),
    )
    body = read(client, session, principal(Persona.SENIOR_MANAGER), f"{SHEET_URL}/versions").json()

    assert [v["version"] for v in body["versions"]] == [1, 2]
    assert [v["is_current"] for v in body["versions"]] == [False, True]


def test_the_history_of_an_artifact_never_submitted_is_empty_not_404(client) -> None:
    """The artifact exists; the honest answer is "nothing has happened to it yet"."""
    response = read(client, scenario(), principal(Persona.SENIOR_MANAGER), f"{SHEET_URL}/versions")

    assert response.status_code == 200
    assert response.json()["versions"] == []


def test_the_history_never_echoes_the_payload(client) -> None:
    """A version row is the lifecycle, not the artifact.

    Echoing a remuneration payload through this API would create a second
    commercial surface with a different policy from `remuneration_sheets`.
    """
    session = scenario(version(ArtifactState.APPROVED, content_hash="a" * 64))
    body = read(client, session, principal(Persona.SENIOR_MANAGER), f"{SHEET_URL}/versions").text

    assert "15484" not in body
    assert "payload" not in body


# =============================================================================
# Registration
# =============================================================================


def test_the_router_is_registered_on_the_app() -> None:
    """`app/main.py` keeps its router list alphabetical; this asserts it is there."""
    from app.main import create_app

    app = create_app()
    paths = app.openapi()["paths"]
    assert any(path.startswith("/approvals") for path in paths)
