"""SEC-06 — `POST /payouts/preview` has no trainer carve-out any more (§4, R5).

CLAUDE.md §4, owner's decision of 2026-08-18: **trainers are records, not users.**
Migration 1800 dropped all eighteen trainer policies, including four writes — two
of which let the payee mark the attendance that decided their own pay. The
`trainer` label survives in `app_role` only as the deny-by-default sentinel that
`handle_new_user()` falls back to for a signup whose role metadata is absent or
malformed, and §4 is explicit that it "grants precisely nothing".

`_require_payout_persona()` kept granting it something. It took a
`trainer_may_read` flag and `POST /payouts/preview` passed `True`, so a caller
holding the sentinel persona could compute a payout — rate, earned, TDS, net — on
the one connection where no policy runs (`app/db/session.py` is BYPASSRLS).

WHY THESE TESTS FAIL WITHOUT THE FIX
------------------------------------
`test_a_trainer_login_is_refused_the_preview` asserts 403 where the old code
returned 200 for a linked trainer, and asserts that the refusal lands before any
row is read — the session raises on ANY access, so the old code fails it twice
over (it reached `session.get(Deployment, ...)`).

`test_the_carve_out_flag_is_gone` is the one that stops it coming back quietly: it
reads the signatures of `_require_payout_persona()` and `_payout_context()` and
fails if either accepts a `trainer_may_read` parameter again. A flag is easy to
re-add in a hurry and hard to notice in review; a named assertion is not.

No database and no full payout fixture is needed. The whole point of the wall is
that it closes before a row is read, so a session that refuses to answer anything
is the strongest possible fixture — if the response is a 403 and the session was
never touched, nothing about the deployment, the rate or the rails was disclosed.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import uuid
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import payouts
from app.api.payouts import _payout_context, _require_payout_persona
from app.core.audit import AuditEvent, AuditWriter, get_audit_writer
from app.core.security import Principal, get_principal
from app.db.session import get_session
from app.domain.enums import Persona

DEPLOYMENT_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
COLLEGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

BODY: dict[str, Any] = {
    "deployment_id": str(DEPLOYMENT_ID),
    "period_start": str(dt.date(2026, 7, 1)),
    "period_end": str(dt.date(2026, 7, 31)),
}

#: Every endpoint in the router, so the refusal is asserted for the whole surface
#: rather than for the one route that carried the flag.
ENDPOINTS = [
    "/payouts/preview",
    "/payouts/validate",
    "/payouts/remuneration-sheet.xlsx",
    "/payouts/invoice-sheet.xlsx",
]


class SealedSession:
    """A session that refuses to answer anything at all.

    R5's "zero rows on a forbidden read", expressed as a fixture: if the guard is
    correct nothing here is ever called, and if it is not, the test fails with the
    name of the row the endpoint tried to read.
    """

    async def get(self, model: type[Any], pk: Any) -> Any:
        raise AssertionError(f"a {model.__name__} row was read before the wall closed")

    async def execute(self, statement: Any) -> Any:
        raise AssertionError("a query was issued before the wall closed")

    async def commit(self) -> None:
        raise AssertionError("a refused request must not commit")


class SilentAudit(AuditWriter):
    async def write(self, event: AuditEvent) -> None:
        raise AssertionError("a refused request must not raise an audit row")

    async def write_within(self, session: Any, event: AuditEvent) -> None:
        raise AssertionError("a refused request must not raise an audit row")


def principal(persona: Persona, colleges: frozenset[uuid.UUID] = frozenset()) -> Principal:
    return Principal(user_id=uuid.uuid4(), persona=persona, college_ids=colleges, is_admin=False)


def client(who: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(payouts.router)
    app.dependency_overrides[get_session] = SealedSession
    app.dependency_overrides[get_principal] = lambda: who
    app.dependency_overrides[get_audit_writer] = lambda: SilentAudit()
    return TestClient(app)


# --- the carve-out is gone -----------------------------------------------------


@pytest.mark.parametrize("url", ENDPOINTS)
def test_a_trainer_login_is_refused_every_payout_endpoint(url: str) -> None:
    """§4: an educator never signs in, so nothing here is theirs to compute.

    `/payouts/preview` is the one that changed; the other three are the controls
    that must not have moved, asserted in the same shape so a future carve-out
    anywhere in this router trips a test.
    """
    response = client(principal(Persona.TRAINER, frozenset({COLLEGE_ID}))).post(url, json=BODY)

    assert response.status_code == 403


def test_a_trainer_login_is_refused_the_preview() -> None:
    """The finding itself, stated on its own so the failure names it.

    Reach is deliberately GRANTED here (`college_ids` is non-empty) to rule out
    the test passing for the wrong reason: the refusal must come from the persona
    wall, not from an empty assignment set.
    """
    response = client(principal(Persona.TRAINER, frozenset({COLLEGE_ID}))).post(
        "/payouts/preview", json=BODY
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Commercial data is not available to your role"


def test_the_guard_refuses_the_trainer_sentinel_directly() -> None:
    """`_require_payout_persona()` in isolation, for every persona §4 lists."""
    for persona in (Persona.TRAINER, Persona.LDE_EXECUTIVE, Persona.COLLEGE):
        with pytest.raises(HTTPException) as caught:
            _require_payout_persona(principal(persona, frozenset({COLLEGE_ID})))
        assert caught.value.status_code == 403, persona


def test_the_two_commercial_personas_still_clear_the_wall() -> None:
    """The control: deleting the carve-out must not have narrowed anything else."""
    for persona in (Persona.MANAGER, Persona.SENIOR_MANAGER):
        _require_payout_persona(principal(persona, frozenset({COLLEGE_ID})))


def test_the_carve_out_flag_is_gone() -> None:
    """The flag itself must not come back, on either function that carried it.

    Deleting a branch is easy to undo by accident; a parameter named in an
    assertion is not. `trainer_may_read=True` was the whole finding, and both
    signatures below are where it would reappear.
    """
    for function in (_require_payout_persona, _payout_context):
        parameters = inspect.signature(function).parameters
        assert "trainer_may_read" not in parameters, function.__name__


def test_no_function_or_call_in_the_module_carries_the_flag() -> None:
    """The flag is gone from the whole module, not just the two signatures above.

    Parsed rather than grepped: the prose in `_require_payout_persona()` names
    `trainer_may_read` deliberately, so that whoever reads the guard next finds
    the argument for why it went. A substring search would forbid the explanation
    along with the parameter, which is the wrong trade — the AST forbids exactly
    the parameter and exactly the keyword argument, and lets the docstring say
    what happened.
    """
    tree = ast.parse(inspect.getsource(payouts))

    parameters = [
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for arg in [*node.args.args, *node.args.kwonlyargs]
    ]
    keywords = [
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    ]

    assert "trainer_may_read" not in parameters
    assert "trainer_may_read" not in keywords


def test_the_only_surviving_trainer_branches_are_refusals() -> None:
    """No live GRANT to `Persona.TRAINER` survives anywhere in the module.

    The two remaining mentions are both inside `_authorised_deployment()`, are
    both refusals, and are documented there as unreachable now that the wall
    closes first. The count is asserted so a third — which would have to be a new
    branch, since both denials already exist — cannot arrive unnoticed.
    """
    source = inspect.getsource(payouts)
    branches = inspect.getsource(payouts._authorised_deployment)

    assert source.count("Persona.TRAINER") == 2
    assert branches.count("Persona.TRAINER") == 2
