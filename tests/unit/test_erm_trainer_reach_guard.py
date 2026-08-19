"""SEC-05 — `app/api/erm.py` must check REACH, not just persona (R5).

`app/db/session.py` connects with BYPASSRLS, so none of 1900's policies runs for a
FastAPI request and `_authorise_trainer()` is the entire wall. It used to return
the moment the caller held a pipeline persona::

    require_internal(principal)
    if _owns_trainer_pipeline(principal):
        return                      # <-- before any reach check

`_owns_trainer_pipeline()` is true for Manager and Senior Manager, so an account
with ZERO rows in either assignment table — the state every fresh internal signup
is in — authorised against ANY `trainer_id`. `POST /erm/tasks` then filed a card
for an arbitrary trainer and `GET /erm/tasks/{id}` returned `_live_pack()`, built
from `TrainerFacts(full_name, pan, email, phone, ...)`.

Migration 2200 fixed the SQL side by giving `can_reach_trainer()` the predicate
"reachable, OR not yet deployed anywhere". These tests pin the Python mirror of
exactly that predicate — **both** disjuncts, because dropping the second one would
close the hole by breaking the sourcing pipeline 1400 and 2200 both argue for.

WHY EACH TEST FAILS WITHOUT THE FIX
-----------------------------------
Every `*_is_refused` case below asserts an `HTTPException(403)` on a call the old
guard satisfied by returning `None`. `pytest.raises` therefore fails outright, and
`test_the_guard_actually_asks_the_deployments_table` fails because the old guard
issued no query at all for a pipeline persona. The two `is_allowed` cases pass on
both versions on purpose: they are the regression fence around the carve-out, so a
later "fix" that simply demanded plain reachability goes red here.

The session is faked rather than mocked out: the guard asks two distinct
questions — which colleges this trainer teaches at (`College`) and whether they are
deployed at all (`Deployment`) — and the fake answers them separately, which is
what lets a test say "deployed somewhere I do not cover" as opposed to "not
deployed".
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import erm
from app.api.erm import _authorise_trainer, _can_reach_trainer
from app.core.audit import AuditEvent, AuditWriter, get_audit_writer
from app.core.security import Principal, get_principal
from app.db.models import College, Deployment, Trainer
from app.db.session import get_session
from app.domain.enums import DocStatus, ErmStatus, Persona
from app.services.erm import ErmSubjectKind, ErmSyncState, ErmSyncTask

MY_COLLEGE = uuid.UUID("11111111-1111-1111-1111-111111111111")
THEIR_COLLEGE = uuid.UUID("22222222-2222-2222-2222-222222222222")
TRAINER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
TASK_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

NOW = dt.datetime(2026, 8, 19, tzinfo=dt.UTC)

PIPELINE_PERSONAS = [Persona.SENIOR_MANAGER, Persona.MANAGER]


# --- fakes ---------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class ReachSession:
    """Answers the two reach questions and refuses every other query.

    Dispatch is on the selected entity, so the fake never has to evaluate a WHERE
    clause: `select(College.id) ...` is the deployment walk and
    `select(Deployment.id) ...` is the existence check. Anything else raises,
    which is how a test learns that the guard started asking a different question.
    """

    def __init__(self, *, colleges: set[uuid.UUID], deployed: bool) -> None:
        self.colleges = colleges
        self.deployed = deployed
        self.asked: list[str] = []

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        self.asked.append(entity.__name__)
        if entity is College:
            return FakeResult(sorted(self.colleges))
        if entity is Deployment:
            return FakeResult([uuid.uuid4()] if self.deployed else [])
        raise AssertionError(f"the reach guard asked an unexpected question: {entity!r}")


def principal(persona: Persona, colleges: frozenset[uuid.UUID] = frozenset()) -> Principal:
    """A verified caller holding a persona and reaching only `colleges`.

    Empty by default: that is the app-side value of `my_college_ids()` for an
    account with no rows in `user_college_assignments` or
    `user_cluster_assignments`, which is what a freshly provisioned Manager is.
    """
    return Principal(user_id=uuid.uuid4(), persona=persona, college_ids=colleges, is_admin=False)


# --- the hole itself -----------------------------------------------------------


@pytest.mark.parametrize("persona", PIPELINE_PERSONAS)
@pytest.mark.parametrize("write", [False, True])
async def test_a_pipeline_persona_with_no_reach_is_refused_a_deployed_trainer(
    persona: Persona, write: bool
) -> None:
    """SEC-05. Zero assignments must not authorise against a deployed trainer.

    Both verbs: `write=True` is `POST /erm/tasks` filing the card, `write=False` is
    `GET /erm/tasks/{id}` handing back PAN, email and phone.
    """
    session = ReachSession(colleges={THEIR_COLLEGE}, deployed=True)

    with pytest.raises(HTTPException) as caught:
        await _authorise_trainer(session, principal(persona), TRAINER_ID, write=write)

    assert caught.value.status_code == 403


@pytest.mark.parametrize("persona", PIPELINE_PERSONAS)
async def test_a_pipeline_persona_is_refused_a_trainer_deployed_elsewhere(
    persona: Persona,
) -> None:
    """Cross-cluster, not merely unassigned: reach to one college is not reach to all."""
    session = ReachSession(colleges={THEIR_COLLEGE}, deployed=True)

    with pytest.raises(HTTPException) as caught:
        await _authorise_trainer(
            session, principal(persona, frozenset({MY_COLLEGE})), TRAINER_ID, write=False
        )

    assert caught.value.status_code == 403


async def test_the_guard_actually_asks_the_deployments_table() -> None:
    """The fix is a query, not a rearranged branch.

    The old guard issued NO query for a pipeline persona. Asserting that both
    questions reach the session pins the shape of 2200's predicate — a future
    edit that drops the `Deployment` check would silently restore "no colleges
    means undeployed", which turns a broken join into an open door.
    """
    session = ReachSession(colleges=set(), deployed=True)

    with pytest.raises(HTTPException):
        await _authorise_trainer(session, principal(Persona.MANAGER), TRAINER_ID, write=False)

    assert session.asked == ["College", "Deployment"]


# --- the carve-out that must survive the fix -----------------------------------


@pytest.mark.parametrize("persona", PIPELINE_PERSONAS)
@pytest.mark.parametrize("write", [False, True])
async def test_an_undeployed_trainer_stays_visible_to_the_pipeline(
    persona: Persona, write: bool
) -> None:
    """2200's second disjunct: "engaged nowhere at all".

    Sourcing and onboarding precede deployment, so a reach-only predicate would
    make a freshly sourced educator invisible to the people whose job is to
    onboard them — and 1400 makes the same argument for the bank rails. Closing
    SEC-05 by demanding plain reachability would be a different defect, so this
    test is the fence around the carve-out rather than a proof of the fix.
    """
    session = ReachSession(colleges=set(), deployed=False)

    await _authorise_trainer(session, principal(persona), TRAINER_ID, write=write)


@pytest.mark.parametrize("persona", PIPELINE_PERSONAS)
async def test_a_pipeline_persona_reaching_the_college_is_allowed(persona: Persona) -> None:
    """The control: covering a college the trainer is engaged at is still enough."""
    session = ReachSession(colleges={MY_COLLEGE, THEIR_COLLEGE}, deployed=True)

    await _authorise_trainer(
        session, principal(persona, frozenset({MY_COLLEGE})), TRAINER_ID, write=True
    )


async def test_can_reach_trainer_is_reachable_or_undeployed() -> None:
    """The predicate on its own, as 2200 words it, in all four combinations."""
    who = principal(Persona.MANAGER, frozenset({MY_COLLEGE}))

    assert await _can_reach_trainer(
        ReachSession(colleges={MY_COLLEGE}, deployed=True), who, TRAINER_ID
    )
    assert await _can_reach_trainer(ReachSession(colleges=set(), deployed=False), who, TRAINER_ID)
    assert not await _can_reach_trainer(
        ReachSession(colleges={THEIR_COLLEGE}, deployed=True), who, TRAINER_ID
    )
    assert not await _can_reach_trainer(
        ReachSession(colleges=set(), deployed=True), who, TRAINER_ID
    )


# --- controls that must not have moved -----------------------------------------


@pytest.mark.parametrize("persona", [Persona.TRAINER, Persona.COLLEGE])
async def test_external_personas_are_still_refused_before_any_query(persona: Persona) -> None:
    """`require_internal()` runs first, so a college login is not an id oracle.

    `session=None` proves the ordering: if any query were issued before the
    persona check this would raise `AttributeError` instead of a 403.
    """
    with pytest.raises(HTTPException) as caught:
        await _authorise_trainer(None, principal(persona), TRAINER_ID, write=False)

    assert caught.value.status_code == 403


async def test_an_lde_executive_still_cannot_write() -> None:
    """Pushing a trainer to ERM is pipeline work, and that is checked before reach."""
    with pytest.raises(HTTPException) as caught:
        await _authorise_trainer(None, principal(Persona.LDE_EXECUTIVE), TRAINER_ID, write=True)

    assert caught.value.status_code == 403
    assert "Senior Manager" in caught.value.detail


async def test_an_lde_executive_gets_no_undeployed_carve_out() -> None:
    """Deliberately STRICTER than 2200's `can_reach_trainer()`, and fail-closed.

    2200 widened that helper for every caller, which incidentally widened 1900's
    `erm_sync_tasks_lde_select_trainer`. An educator engaged nowhere is on no
    campus, so "is this trainer on my campus synced" has no answer for them; the
    campus read stays bound to an actual deployment, which is what this endpoint
    already did before the fix.
    """
    session = ReachSession(colleges=set(), deployed=False)

    with pytest.raises(HTTPException) as caught:
        await _authorise_trainer(
            session,
            principal(Persona.LDE_EXECUTIVE, frozenset({MY_COLLEGE})),
            TRAINER_ID,
            write=False,
        )

    assert caught.value.status_code == 403


# --- the same hole, over HTTP: what the pack would have handed over ------------
#
# The guard tests above are the precise ones. These two are the consequence: an
# end-to-end assertion that the identity fields SEC-05 names — full name, PAN,
# email, phone — do not appear in any response body for a caller with no reach,
# and that the card cannot be filed in the first place.


class ErmSession(ReachSession):
    """`ReachSession` plus primary-key lookups, for driving the router.

    The trainer row IS loaded, with a real PAN on it, so a passing test is
    evidence that the guard refused rather than evidence that the fixture was
    empty. `_live_pack()` would read exactly these columns.
    """

    def __init__(self, *rows: Any, colleges: set[uuid.UUID], deployed: bool) -> None:
        super().__init__(colleges=colleges, deployed=deployed)
        self.rows = list(rows)
        self.commits = 0

    async def get(self, model: type[Any], pk: Any) -> Any:
        return next((r for r in self.rows if isinstance(r, model) and r.id == pk), None)

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        if entity is ErmSyncTask:
            self.asked.append(entity.__name__)
            return FakeResult([r for r in self.rows if isinstance(r, ErmSyncTask)])
        return await super().execute(statement)

    def add(self, obj: Any) -> None:
        self.rows.append(obj)

    async def commit(self) -> None:  # pragma: no cover - a refused request never commits
        self.commits += 1


class SilentAudit(AuditWriter):
    async def write(self, event: AuditEvent) -> None:  # pragma: no cover - never reached
        raise AssertionError("a refused request must not raise an audit row")

    async def write_within(self, session: Any, event: AuditEvent) -> None:  # pragma: no cover
        raise AssertionError("a refused request must not raise an audit row")


def erm_client(session: ErmSession, who: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(erm.router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_principal] = lambda: who
    app.dependency_overrides[get_audit_writer] = lambda: SilentAudit()
    return TestClient(app)


def deployed_trainer() -> Trainer:
    """A trainer engaged at a college the caller does not cover."""
    return Trainer(
        id=TRAINER_ID,
        pan="FKYPM5666Z",
        full_name="Vema",
        email="vema@example.invalid",
        phone="9059433134",
        type="freelancer",
        work_order_status=DocStatus.SIGNED,
        zoho_id="ZH-1",
        erm_status=ErmStatus.NOT_PUSHED,
        created_at=NOW,
        updated_at=NOW,
    )


def open_card() -> ErmSyncTask:
    return ErmSyncTask(
        id=TASK_ID,
        subject_kind=ErmSubjectKind.TRAINER,
        trainer_id=TRAINER_ID,
        program_id=None,
        state=ErmSyncState.QUEUED,
        field_order_version=1,
        verified=False,
        created_at=NOW,
        updated_at=NOW,
    )


def test_reading_a_card_out_of_reach_returns_403_and_no_identity_fields() -> None:
    """R5, in the form the finding describes: the field pack must not leave."""
    session = ErmSession(open_card(), deployed_trainer(), colleges={THEIR_COLLEGE}, deployed=True)
    response = erm_client(session, principal(Persona.MANAGER)).get(f"/erm/tasks/{TASK_ID}")

    assert response.status_code == 403
    body = response.text
    for secret in ("FKYPM5666Z", "9059433134", "vema@example.invalid"):
        assert secret not in body


def test_the_queue_listing_drops_a_card_out_of_reach() -> None:
    """`GET /erm/tasks` filters per row, so an unreachable card is simply absent."""
    session = ErmSession(open_card(), deployed_trainer(), colleges={THEIR_COLLEGE}, deployed=True)
    response = erm_client(session, principal(Persona.MANAGER)).get("/erm/tasks")

    assert response.status_code == 200
    assert response.json()["tasks"] == []


def test_filing_a_card_for_a_trainer_out_of_reach_is_refused() -> None:
    """`POST /erm/tasks` is the write half, and it is refused before the row is read."""
    session = ErmSession(deployed_trainer(), colleges={THEIR_COLLEGE}, deployed=True)
    response = erm_client(session, principal(Persona.MANAGER)).post(
        "/erm/tasks", json={"subject_kind": "trainer", "subject_id": str(TRAINER_ID)}
    )

    assert response.status_code == 403
    assert session.commits == 0


def test_the_queue_asks_the_reach_question_once_per_subject() -> None:
    """The listing must not re-derive the same verdict for every card on a record.

    A record edited repeatedly carries one open card and a tail of confirmed and
    stale ones, so a page is routinely several cards about the same trainer. Since
    SEC-05 each trainer verdict costs up to two queries where a pipeline persona
    used to cost none, and `MAX_PAGE` is 200 — asking per row would turn a queue
    read into four hundred round trips to close a hole that one answer closes.
    """
    cards = [open_card(), open_card(), open_card()]
    for index, card in enumerate(cards):
        card.id = uuid.uuid4()
        card.created_at = NOW + dt.timedelta(minutes=index)
    session = ErmSession(*cards, deployed_trainer(), colleges={MY_COLLEGE}, deployed=True)

    response = erm_client(session, principal(Persona.MANAGER, frozenset({MY_COLLEGE}))).get(
        "/erm/tasks"
    )

    assert len(response.json()["tasks"]) == 3
    assert session.asked.count("College") == 1
