"""SEC-08 — `GET /erm/tasks` must limit what the caller may SEE.

The listing used to hand `LIMIT` to Postgres and then drop out-of-reach cards in
Python::

    rows = ... .order_by(created_at).limit(limit)
    for row in rows:
        try: await _authorise(...)     # <-- after the database already cut the page
        except HTTPException: continue

so `limit` meant "rows considered", not "rows returned". A caller asking for
fifty cards got fewer than fifty while more sat waiting behind the ones dropped,
with nothing in the response saying so. It was already true for an LDE Executive;
SEC-05 gave a Manager and a Senior Manager a reach they can fall outside of too,
so it stopped being one persona's problem.

Under-disclosure, not over-disclosure: the wall never moved and no unreachable
card was ever returned. It still matters — this queue is what somebody reads to
decide which record to retype into ERM next, and a queue that silently hides work
is a queue people stop trusting.

WHY THIS WAS HARD, AND WHAT WAS DONE ABOUT IT
=============================================
`app/api/comms.py` had the same defect and the fix was a one-liner, because its
wall is a column on the row (`is_commercial`). This one is a JOIN and a
persona-dependent carve-out — 2200/2500's "reachable, OR not yet deployed
anywhere" — so pushing it into the `WHERE` risked ending up with the
authorisation rule written twice, in Python and in SQL, free to drift apart.

The router's answer is to write the rule ONCE in three pieces and evaluate it
twice::

    _trainer_colleges()      the deployments -> batches -> programs -> colleges walk
    _trainer_deployments()   "is this trainer engaged anywhere at all"
    _trainer_reach_rule()    the boolean that combines them, carve-out included

`_trainer_college_ids()` / `_trainer_is_deployed()` execute the first two against
one id; `_visible_to()` correlates the same statements into `EXISTS` clauses over
the queue row. `_trainer_reach_rule()` is called with `bool`s by the guard and
with SQL expressions by the listing — one function, because `|` is OR in both.

**The three `..._share_...` tests below are the anti-drift assertion.** They spy
on each piece and fail unless BOTH paths go through it. Re-inline the walk or the
boolean on either side and a test goes red, rather than the counts going quietly
wrong again.

WHY EACH TEST FAILS WITHOUT THE FIX
-----------------------------------
`test_a_full_page_of_reachable_cards_comes_back` builds sixty cards: the fifty
OLDEST are out of reach and the ten newest are not, then asks for ten. Before the
fix the database returned the fifty oldest, the Python pass dropped all fifty, and
the response held **zero** cards while ten existed. `test_the_reach_is_applied_in_sql`
asserts the same thing structurally, on the statement the router actually issues,
so the behavioural test cannot later be satisfied by a Python over-fetch.

WHAT THE FAKE DOES AND DOES NOT PROVE
-------------------------------------
`QueueSession` evaluates only what this endpoint depends on — the reach
predicate, `ORDER BY created_at`, then `LIMIT` — in the order Postgres applies
them, and it decides reach from the `Estate` fixture using the rule as this file
states it, independently of how the router spells it. That proves the FILTER
HAPPENS BEFORE THE LIMIT. It does not prove the compiled SQL means what the
router thinks; nothing short of a real Postgres could, and
`app/db/models.py` forbids standing one up from the models. The structural and
spy tests carry that half, and `test_the_python_pass_still_refuses_what_sql_let_through`
carries the safety half — the Python guard stays the wall, so the worst a
divergence in the SQL can do is return too few rows.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api import erm
from app.core.audit import AuditEvent, AuditWriter, get_audit_writer
from app.core.security import Principal, get_principal
from app.db.models import College, Deployment, Program, Trainer
from app.db.session import get_session
from app.domain.enums import DocStatus, ErmStatus, Persona, ProgramStage, ProgramType
from app.services.erm import ErmSubjectKind, ErmSyncState, ErmSyncTask

MY_COLLEGE = uuid.UUID("11111111-1111-1111-1111-111111111111")
THEIR_COLLEGE = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

NOW = dt.datetime(2026, 8, 19, tzinfo=dt.UTC)

#: The two personas that hold the trainer pipeline, and therefore the carve-out.
PIPELINE_PERSONAS = [Persona.SENIOR_MANAGER, Persona.MANAGER]

#: Every persona `require_internal()` lets as far as the reach predicate.
INTERNAL_PERSONAS = [*PIPELINE_PERSONAS, Persona.LDE_EXECUTIVE]


# --- the world the queue sits in ------------------------------------------------


@dataclass
class Estate:
    """Which trainers teach where, which programs belong to which college.

    The two trainer facts are kept apart on purpose, because 2200 keeps them
    apart: `deployed` is asked of `deployments` directly, so a deployment whose
    batch or program row has gone missing is "engaged somewhere I cannot see"
    rather than "engaged nowhere" — a broken join must not open the carve-out.
    """

    trainer_colleges: dict[uuid.UUID, frozenset[uuid.UUID]] = field(default_factory=dict)
    deployed: set[uuid.UUID] = field(default_factory=set)
    program_college: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)

    def trainer(self, *, colleges: frozenset[uuid.UUID], deployed: bool) -> uuid.UUID:
        trainer_id = uuid.uuid4()
        self.trainer_colleges[trainer_id] = colleges
        if deployed:
            self.deployed.add(trainer_id)
        return trainer_id

    def program(self, college_id: uuid.UUID) -> uuid.UUID:
        program_id = uuid.uuid4()
        self.program_college[program_id] = college_id
        return program_id


def expected_visible(estate: Estate, principal: Principal, row: ErmSyncTask) -> bool:
    """The rule as THIS FILE states it — 1900's three policies, read for a listing.

    Written out independently of `app/api/erm.py` so the fake is not merely
    agreeing with the router about itself. It is the same rule the module
    docstring quotes: a trainer card is visible when the caller covers a college
    the trainer is engaged at, or — for a pipeline persona only — when the trainer
    is engaged nowhere; a program card is visible when its program exists and its
    college is in reach.
    """
    if row.subject_kind is ErmSubjectKind.TRAINER:
        assert row.trainer_id is not None
        reachable = bool(
            estate.trainer_colleges.get(row.trainer_id, frozenset()) & principal.college_ids
        )
        if principal.persona is Persona.LDE_EXECUTIVE:
            return reachable
        return reachable or row.trainer_id not in estate.deployed
    college_id = estate.program_college.get(row.program_id) if row.program_id else None
    return college_id is not None and college_id in principal.college_ids


# --- fakes ----------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class QueueSession:
    """In-memory stand-in that applies filter, then order, then limit.

    That order is the whole finding. `_limit` and the `WHERE` are read off the
    statement the router built rather than simulated from the router's arguments,
    so the test is measuring what would actually reach Postgres.

    `wide=True` makes the fake ignore the reach predicate entirely — a stand-in
    for a `_visible_to()` that has drifted too permissive. The response must still
    contain nothing out of reach, because the Python pass is the wall.
    """

    def __init__(
        self,
        estate: Estate,
        principal: Principal,
        *rows: Any,
        wide: bool = False,
    ) -> None:
        self.estate = estate
        self.principal = principal
        self.rows = list(rows)
        self.wide = wide
        self.wheres: list[str] = []

    async def get(self, model: type[Any], pk: Any) -> Any:
        return next((row for row in self.rows if isinstance(row, model) and row.id == pk), None)

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        if entity is ErmSyncTask:
            return self._queue(statement)
        if entity is College:
            return FakeResult(
                sorted(self.estate.trainer_colleges.get(_bound(statement), frozenset()))
            )
        if entity is Deployment:
            deployed = _bound(statement) in self.estate.deployed
            return FakeResult([uuid.uuid4()] if deployed else [])
        raise AssertionError(f"the listing asked an unexpected question: {entity!r}")

    def _queue(self, statement: Any) -> FakeResult:
        where = "" if statement.whereclause is None else str(statement.whereclause)
        self.wheres.append(where)

        rows = [row for row in self.rows if isinstance(row, ErmSyncTask)]
        if not self.wide and "deployments.trainer_id" in where:
            rows = [r for r in rows if expected_visible(self.estate, self.principal, r)]
        rows.sort(key=lambda r: r.created_at)
        if statement._limit is not None:
            rows = rows[: statement._limit]
        return FakeResult(rows)


def _bound(statement: Any) -> uuid.UUID:
    """The trainer id a per-card guard bound into its `WHERE`.

    Both guard statements are `... where deployments.trainer_id = :id`, so the
    right-hand bind is the subject. Reading it lets one fake answer for several
    trainers, which is what a listing needs and a single-card guard test does not.
    """
    bound = statement.whereclause.right.value
    assert isinstance(bound, uuid.UUID)
    return bound


class SilentAudit(AuditWriter):
    async def write(self, event: AuditEvent) -> None:  # pragma: no cover - a listing is a pure read
        raise AssertionError("a pure read must not raise an audit row")

    async def write_within(self, session: Any, event: AuditEvent) -> None:  # pragma: no cover
        raise AssertionError("a pure read must not raise an audit row")


# --- fixtures -------------------------------------------------------------------


def card(
    *,
    trainer_id: uuid.UUID | None = None,
    program_id: uuid.UUID | None = None,
    created_at: dt.datetime = NOW,
) -> ErmSyncTask:
    """One job card. Only the columns the listing orders, filters and serialises."""
    kind = ErmSubjectKind.TRAINER if trainer_id is not None else ErmSubjectKind.PROGRAM
    return ErmSyncTask(
        id=uuid.uuid4(),
        subject_kind=kind,
        trainer_id=trainer_id,
        program_id=program_id,
        state=ErmSyncState.QUEUED,
        field_order_version=1,
        verified=False,
        created_at=created_at,
        updated_at=created_at,
    )


def trainer_row(trainer_id: uuid.UUID) -> Trainer:
    """A real trainer row, with a real PAN on it.

    Loaded so that a passing test is evidence the reach refused, not evidence the
    fixture was empty: `_live_pack()` reads exactly these columns.
    """
    return Trainer(
        id=trainer_id,
        pan="FKYPM5666Z",
        full_name="VEMA PRUDHVI SAI",
        email="vema@example.invalid",
        phone="9059433134",
        type="freelancer",
        work_order_status=DocStatus.SIGNED,
        zoho_id="ZH-1",
        erm_status=ErmStatus.NOT_PUSHED,
        created_at=NOW,
        updated_at=NOW,
    )


def program_row(program_id: uuid.UUID, college_id: uuid.UUID) -> Program:
    return Program(
        id=program_id,
        college_id=college_id,
        type=ProgramType.BCAP,
        name="bCAP CSE-A 2026",
        start_date=dt.date(2026, 7, 1),
        end_date=dt.date(2026, 7, 31),
        stage=ProgramStage.TRAINER_ONBOARDING,
        created_at=NOW,
        updated_at=NOW,
    )


def principal(
    persona: Persona, colleges: frozenset[uuid.UUID] = frozenset({MY_COLLEGE})
) -> Principal:
    return Principal(user_id=USER_ID, persona=persona, college_ids=colleges, is_admin=False)


def client(session: QueueSession, who: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(erm.router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_principal] = lambda: who
    app.dependency_overrides[get_audit_writer] = lambda: SilentAudit()
    return TestClient(app)


def queue(
    who: Principal,
    *,
    out_of_reach: int,
    reachable: int,
    sourced: int = 0,
    wide: bool = False,
) -> QueueSession:
    """A queue whose `out_of_reach` OLDEST cards sit in front of `reachable` ones.

    The interleaving is the scenario. This listing is oldest-first, so filtering
    after `LIMIT` returns nothing at all when the oldest page is entirely out of
    reach — which is what a Manager covering one cluster of a large estate sees.

    `sourced` adds cards for trainers engaged NOWHERE: 2200's carve-out, visible to
    a pipeline persona and to nobody else. They are the only cards that make the
    guard ask the deployments question, because a reachable trainer answers on the
    walk alone.

    Every card names a DIFFERENT trainer, deliberately: cards about one record
    share a memoised verdict, and a page of distinct subjects is both the honest
    worst case and the only shape that makes the row count meaningful.
    """
    estate = Estate()
    rows: list[Any] = []

    def add(trainer_id: uuid.UUID, at: dt.datetime) -> None:
        rows.append(trainer_row(trainer_id))
        rows.append(card(trainer_id=trainer_id, created_at=at))

    for index in range(out_of_reach):
        add(
            estate.trainer(colleges=frozenset({THEIR_COLLEGE}), deployed=True),
            NOW + dt.timedelta(minutes=index),
        )
    for index in range(sourced):
        add(
            estate.trainer(colleges=frozenset(), deployed=False),
            NOW + dt.timedelta(minutes=30, seconds=index),
        )
    for index in range(reachable):
        add(
            estate.trainer(colleges=frozenset({MY_COLLEGE}), deployed=True),
            NOW + dt.timedelta(hours=1, minutes=index),
        )
    return QueueSession(estate, who, *rows, wide=wide)


def compiled(who: Principal) -> str:
    """The `WHERE` the router would send to Postgres, as text."""
    statement = select(ErmSyncTask).where(erm._visible_to(who))
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


# --- the finding ----------------------------------------------------------------


@pytest.mark.parametrize("persona", INTERNAL_PERSONAS)
def test_a_full_page_of_reachable_cards_comes_back(persona: Persona) -> None:
    """Ten reachable cards exist and ten were asked for, so ten come back.

    Before the fix: the fifty oldest rows were out of reach, the database returned
    those, the Python pass dropped every one, and the caller saw an empty queue
    with fifty of their own cards behind it.

    Parametrised over all three internal personas because SEC-05 is what made this
    a Manager's problem as well as an LDE Executive's.
    """
    who = principal(persona)
    session = queue(who, out_of_reach=50, reachable=10)

    response = client(session, who).get("/erm/tasks?limit=10")

    assert response.status_code == 200
    assert len(response.json()["tasks"]) == 10


def test_the_page_is_still_capped_at_the_limit() -> None:
    """The fix must not turn the limit into a suggestion."""
    who = principal(Persona.MANAGER)
    session = queue(who, out_of_reach=0, reachable=25)

    response = client(session, who).get("/erm/tasks?limit=10")

    assert len(response.json()["tasks"]) == 10


def test_the_reach_is_applied_in_sql_not_after_the_limit() -> None:
    """Structural: the predicate must be in the statement the router issues.

    A behavioural test alone would pass again if somebody moved the filter back
    into Python and merely over-fetched. This one names where the predicate has to
    live, because `LIMIT` is applied by the database and so must the filter be.
    """
    who = principal(Persona.MANAGER)
    session = queue(who, out_of_reach=2, reachable=2)

    client(session, who).get("/erm/tasks")

    assert session.wheres, "the listing issued no statement against erm_sync_tasks"
    assert all("deployments.trainer_id" in where for where in session.wheres)
    assert all("erm_sync_tasks.subject_kind" in where for where in session.wheres)


def test_a_program_card_out_of_reach_never_reaches_the_page() -> None:
    """The other subject kind, and the other half of the `OR`.

    A program card's reach is its college, so an unreachable one must be excluded
    by the same statement rather than counted against the limit and then dropped.
    """
    who = principal(Persona.MANAGER)
    estate = Estate()
    mine = estate.program(MY_COLLEGE)
    theirs = estate.program(THEIR_COLLEGE)
    session = QueueSession(
        estate,
        who,
        program_row(mine, MY_COLLEGE),
        program_row(theirs, THEIR_COLLEGE),
        card(program_id=theirs, created_at=NOW),
        card(program_id=mine, created_at=NOW + dt.timedelta(hours=1)),
    )

    body = client(session, who).get("/erm/tasks?limit=1").json()["tasks"]

    assert [row["subject_id"] for row in body] == [str(mine)]
    assert "programs.college_id" in compiled(who)


# --- the anti-drift assertion: one rule, two evaluations ------------------------
#
# The rule now exists in a `WHERE` and in a per-subject guard. These three tests
# are what stops that being two rules. Each spies on one of the three shared
# pieces and asserts BOTH evaluations went through it: the listing calls it with
# a SQL expression while building the statement, the guard calls it with a
# concrete id while checking the rows that came back. A re-inlined walk or a
# hand-written `or` on either side stops one of these seeing its call.


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Any]:
    """Wrap `erm.<name>`, recording the first positional argument of every call."""
    original = getattr(erm, name)
    seen: list[Any] = []

    def recorder(first: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(first)
        return original(first, *args, **kwargs)

    monkeypatch.setattr(erm, name, recorder)
    return seen


def _drive_both_paths(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Any]:
    """One listing request, which builds the `WHERE` and then guards every row.

    The page holds a reachable trainer AND a sourced-but-undeployed one, so the
    guard exercises both disjuncts: the first answers on the walk, the second
    forces the deployments question the carve-out turns on.
    """
    seen = _spy(monkeypatch, name)
    who = principal(Persona.MANAGER)
    session = queue(who, out_of_reach=1, reachable=1, sourced=1)
    assert client(session, who).get("/erm/tasks").status_code == 200
    return seen


def test_the_listing_and_the_guard_share_the_deployment_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_trainer_colleges()` is called by both, or the reach is two reaches.

    A `UUID` argument is the guard asking about one trainer; anything else is the
    listing correlating the same statement against `erm_sync_tasks.trainer_id`.
    """
    seen = _drive_both_paths(monkeypatch, "_trainer_colleges")

    assert any(isinstance(arg, uuid.UUID) for arg in seen), "the guard did not use the walk"
    assert any(not isinstance(arg, uuid.UUID) for arg in seen), "the listing did not use the walk"


def test_the_listing_and_the_guard_share_the_deployment_existence_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_trainer_deployments()` is the carve-out's half, and it is shared too.

    2200 asks `deployments` directly rather than inferring "undeployed" from an
    empty walk, and a second copy of that question is exactly where the two would
    drift — one of them "fixed" to use the walk, quietly turning a broken join
    into an open door.
    """
    seen = _drive_both_paths(monkeypatch, "_trainer_deployments")

    assert any(isinstance(arg, uuid.UUID) for arg in seen), "the guard did not ask deployments"
    assert any(
        not isinstance(arg, uuid.UUID) for arg in seen
    ), "the listing did not ask deployments"


def test_the_listing_and_the_guard_share_the_reach_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_trainer_reach_rule()` is the only place the carve-out is spelled.

    The guard hands it `bool`s, the listing hands it SQL expressions, and `|` is
    OR in both. If either side ever writes its own `or`, one of these two
    assertions stops holding.
    """
    seen = _drive_both_paths(monkeypatch, "_trainer_reach_rule")

    assert any(isinstance(arg, bool) for arg in seen), "the guard did not use the rule"
    assert any(not isinstance(arg, bool) for arg in seen), "the listing did not use the rule"


def test_the_walk_in_the_where_is_the_walk_the_guard_runs() -> None:
    """Structural companion to the spies: the shared statement lands verbatim.

    The spies prove the function was called; this proves its output is what went
    into the `WHERE`, rather than being built and then discarded in favour of a
    hand-rolled join. The join chain is compared rather than the whole statement
    because the correlated form drops `erm_sync_tasks` out of its own `FROM` —
    which is the correlation working, and is asserted separately below it.
    """
    walk = str(erm._trainer_colleges(uuid.uuid4()).compile(dialect=postgresql.dialect()))
    joins = walk[walk.index("FROM colleges") : walk.index("WHERE")].strip()
    where = compiled(principal(Persona.MANAGER))

    assert joins in where
    assert "WHERE deployments.trainer_id = erm_sync_tasks.trainer_id" in where


# --- the rule itself: both algebras, every shape --------------------------------


@pytest.mark.parametrize("reachable", [True, False])
@pytest.mark.parametrize("undeployed", [True, False])
@pytest.mark.parametrize("carve_out", [True, False])
def test_the_rule_is_reachable_or_undeployed(
    reachable: bool, undeployed: bool, carve_out: bool
) -> None:
    """2200/2500's predicate, in `bool`, in all eight shapes.

    The carve-out is the caller's, not the trainer's: a pipeline persona gets both
    disjuncts, an LDE Executive gets only reachability, because an educator
    engaged nowhere is on no campus.
    """
    verdict = erm._trainer_reach_rule(reachable, undeployed, carve_out=carve_out)

    assert verdict is (reachable or (carve_out and undeployed))


@pytest.mark.parametrize("persona", PIPELINE_PERSONAS)
def test_the_sourcing_carve_out_survives_into_the_statement(persona: Persona) -> None:
    """A trainer engaged nowhere stays visible to the pipeline — in SQL as well.

    Closing this by demanding plain reachability would be a different defect: 2200
    exists because onboarding precedes deployment, and a freshly sourced educator
    must stay visible to the people onboarding them.
    """
    where = compiled(principal(persona))

    assert "NOT (EXISTS" in where
    assert "FROM deployments" in where


def test_an_lde_executive_gets_no_carve_out_in_the_statement() -> None:
    """Deliberately STRICTER than `can_reach_trainer()`, and fail-closed.

    The campus read stays bound to an actual deployment, which is what
    `_authorise_trainer()` does per card. The `WHERE` must not be wider than the
    guard, or the listing offers cards the detail endpoint then refuses.
    """
    assert "NOT (EXISTS" not in compiled(principal(Persona.LDE_EXECUTIVE))


def test_the_statement_carries_migration_2500s_null_guards() -> None:
    """`not exists (... where trainer_id = null)` is TRUE. 2500 is that bug.

    `erm_sync_tasks_subject_ck` means a trainer card always has a `trainer_id`, so
    the `subject_kind` conjunct already holds the line — and 2500's whole argument
    is that leaving a reach predicate honest only because of a column constraint
    is how the next caller gets hurt.
    """
    where = compiled(principal(Persona.MANAGER))

    assert "erm_sync_tasks.trainer_id IS NOT NULL" in where
    assert "erm_sync_tasks.program_id IS NOT NULL" in where


def test_a_caller_with_no_assignments_is_offered_only_undeployed_trainers() -> None:
    """Deny-by-default, in the statement: empty reach is empty, not everything.

    A freshly provisioned Manager has zero rows in either assignment table, which
    is the state SEC-05 was found in. The carve-out still applies — that is 2200 —
    but nothing else does.
    """
    who = principal(Persona.MANAGER, frozenset())
    estate = Estate()
    engaged = estate.trainer(colleges=frozenset({THEIR_COLLEGE}), deployed=True)
    sourced = estate.trainer(colleges=frozenset(), deployed=False)
    session = QueueSession(
        estate,
        who,
        trainer_row(engaged),
        trainer_row(sourced),
        card(trainer_id=engaged, created_at=NOW),
        card(trainer_id=sourced, created_at=NOW + dt.timedelta(hours=1)),
    )

    body = client(session, who).get("/erm/tasks?limit=50").json()["tasks"]

    assert [row["subject_id"] for row in body] == [str(sourced)]


# --- the backstop: Python is still the wall -------------------------------------


@pytest.mark.parametrize("persona", INTERNAL_PERSONAS)
def test_the_python_pass_still_refuses_what_sql_let_through(persona: Persona) -> None:
    """R5 on a BYPASSRLS connection: the statement is a filter, the guard is the wall.

    `wide=True` simulates a `_visible_to()` that has drifted too permissive — every
    card comes back from the database regardless of the predicate. The response
    must still hold nothing out of reach, and none of the identity fields the pack
    carries may appear anywhere in it.

    This is why the Python pass is kept rather than replaced: it makes the failure
    asymmetric. A divergence in the SQL costs rows, never secrets.
    """
    who = principal(persona)
    session = queue(who, out_of_reach=20, reachable=0, wide=True)

    response = client(session, who).get("/erm/tasks?limit=50")

    assert response.status_code == 200
    assert response.json()["tasks"] == []
    for secret in ("FKYPM5666Z", "9059433134", "vema@example.invalid"):
        assert secret not in response.text


def test_the_guard_verdict_is_still_asked_once_per_subject() -> None:
    """The pre-filter must not have turned the backstop into a per-row query storm.

    A record edited repeatedly carries one open card and a tail of confirmed and
    stale ones, so a page is routinely several cards about one trainer. The verdict
    is memoised per subject; `MAX_PAGE` is 200, and asking per row would be four
    hundred round trips to re-derive one answer.
    """
    who = principal(Persona.MANAGER)
    estate = Estate()
    trainer_id = estate.trainer(colleges=frozenset({MY_COLLEGE}), deployed=True)
    cards = [
        card(trainer_id=trainer_id, created_at=NOW + dt.timedelta(minutes=index))
        for index in range(3)
    ]
    session = QueueSession(estate, who, trainer_row(trainer_id), *cards)
    asked: list[Any] = []
    original = erm._trainer_college_ids

    async def counting(session_: Any, trainer: uuid.UUID) -> set[uuid.UUID]:
        asked.append(trainer)
        return await original(session_, trainer)

    erm._trainer_college_ids = counting  # type: ignore[assignment]
    try:
        body = client(session, who).get("/erm/tasks").json()["tasks"]
    finally:
        erm._trainer_college_ids = original  # type: ignore[assignment]

    assert len(body) == 3
    assert asked == [trainer_id]


def test_an_external_persona_is_refused_before_any_statement_is_built() -> None:
    """`require_internal()` runs first, so a college login is not an id oracle.

    Ordering matters as much here as in the guards: the reach predicate is built
    from the caller's persona, and building it for somebody who may not read this
    queue at all would be a query issued on their behalf.
    """
    who = principal(Persona.COLLEGE)
    session = queue(who, out_of_reach=0, reachable=3)

    response = client(session, who).get("/erm/tasks")

    assert response.status_code == 403
    assert session.wheres == []


def test_the_response_shape_is_unchanged() -> None:
    """`frontend/src/lib/erm.ts` was written against this contract.

    The fix is a `WHERE` clause. Nothing about what a card looks like on the wire
    moved, and a page still carries the field-order warning at the collection
    level so no client can render a queue without having been told the order is
    provisional.
    """
    who = principal(Persona.MANAGER)
    session = queue(who, out_of_reach=0, reachable=1)

    body = client(session, who).get("/erm/tasks").json()

    assert set(body) == {"tasks", "field_order_version", "field_order_verified"}
    assert body["field_order_verified"] is False
    assert set(body["tasks"][0]) >= {"id", "subject_kind", "subject_id", "subject_label", "state"}


def test_the_filters_still_compose_with_the_reach_predicate() -> None:
    """`state`, `subject_kind` and `assigned_to_me` are ANDed, not replaced.

    The reach predicate is the first `where()` on the statement; a later filter
    that dropped it would be a silent widening, and one that replaced the ordering
    would put the limit back in front of the filter.
    """
    who = principal(Persona.MANAGER)
    session = queue(who, out_of_reach=2, reachable=2)

    client(session, who).get("/erm/tasks?state=queued&subject_kind=trainer&assigned_to_me=true")

    where = session.wheres[-1]
    assert "deployments.trainer_id" in where
    assert "erm_sync_tasks.state" in where
    assert "erm_sync_tasks.assigned_to" in where


def test_the_listing_refuses_nothing_it_can_partly_answer() -> None:
    """A queue read is filtered, never refused — 1900's policies return rows, not errors.

    A Manager and an LDE Executive both legitimately read this queue and should
    see different rows in it. Asserted here because the SQL predicate is now the
    first thing that could turn a partial answer into a 403.
    """
    for persona in INTERNAL_PERSONAS:
        who = principal(persona)
        session = queue(who, out_of_reach=5, reachable=0)

        response = client(session, who).get("/erm/tasks")

        assert response.status_code == 200, persona
        assert response.json()["tasks"] == []
