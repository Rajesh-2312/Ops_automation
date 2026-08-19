"""SEC-05 — the FastAPI mirror of SEC-02, in `app/api/erm.py`.

`app/db/session.py` connects with a BYPASSRLS credential, so for every FastAPI
route the Python guard IS the wall. `app/api/erm.py:435` mirrors 1900's
`erm_sync_tasks_sourcing_all` policy faithfully — including its missing reach
conjunct::

    async def _authorise_trainer(session, principal, trainer_id, *, write) -> None:
        require_internal(principal)
        if _owns_trainer_pipeline(principal):
            return                      # <-- returns before any reach check
        ...

`_owns_trainer_pipeline()` is true for Senior Manager and Manager, so a caller
with ZERO college assignments passes for ANY `trainer_id`. The consequence is not
abstract: `POST /erm/tasks` files a card for an arbitrary trainer, and
`GET /erm/tasks/{id}` then returns `_live_pack()`, which is built from
`TrainerFacts(full_name, pan, email, phone, ...)` — the trainer's tax identity and
contact details, for any trainer in the estate.

These tests need no database and no HTTP: the gap is in a pure guard function, so
it is probed directly with a synthetic `Principal`. That also means they keep
working if the endpoint is refactored, as long as the guard keeps its name.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.api.erm import _authorise_trainer, _owns_trainer_pipeline
from app.core.security import Principal
from app.domain.enums import Persona


class _DeployedTrainerSession:
    """The narrowest fake that lets the guard ask its two questions.

    Both `_trainer_college_ids()` and `_trainer_is_deployed()` run
    `session.execute(...)` and read `.scalars().all()`. Returning one non-empty
    row answers "this trainer IS deployed" and "the college it is deployed at is
    <some id the caller does not hold>" at once — which is precisely the case
    SEC-05 was about: a real, deployed trainer belonging to somebody else.

    WHY THIS EXISTS AT ALL. These tests used to pass `session=None`, and that was
    correct while the bug was live: the guard short-circuited on persona before
    touching a session, so `None` proved the short-circuit. Once the fix landed,
    `None` made the guard raise `AttributeError` instead of `HTTPException` —
    and `xfail(strict=True)` counts *any* failure as "failed as expected", so
    these four cases would have sat at XFAIL forever and the suite would have
    reported SEC-05 as still open long after it was closed. A strict-xfail that
    can fail for the wrong reason silently stops being a tripwire.
    """

    class _Result:
        @staticmethod
        def scalars() -> _DeployedTrainerSession._Scalars:
            return _DeployedTrainerSession._Scalars()

    class _Scalars:
        @staticmethod
        def all() -> list[uuid.UUID]:
            # A college the zero-reach principal does not hold.
            return [uuid.UUID("dead0000-0000-4000-8000-00000000dead")]

    async def execute(self, _statement: object) -> _DeployedTrainerSession._Result:
        return self._Result()


def _principal(persona: Persona) -> Principal:
    """A verified caller holding a persona and reaching NOTHING.

    `college_ids` empty is the app-side value of `my_college_ids()` for an account
    with no rows in either assignment table — the state every fresh signup is in.
    """
    return Principal(
        user_id=uuid.uuid4(),
        persona=persona,
        college_ids=frozenset(),
        is_admin=False,
    )


# SEC-05 CLOSED: `_authorise_trainer()` now mirrors migration 2200's
# `can_reach_trainer(uuid)` — "reachable, or not yet deployed anywhere" — instead
# of returning on persona alone. The xfail(strict=True) that stood here is
# deleted rather than skipped; see `_DeployedTrainerSession` for why it could no
# longer be trusted to flip.
@pytest.mark.parametrize("persona", [Persona.MANAGER, Persona.SENIOR_MANAGER])
@pytest.mark.parametrize("write", [False, True])
async def test_pipeline_persona_without_reach_is_refused(persona: Persona, write: bool) -> None:
    """A caller reaching no college must not authorise against an arbitrary trainer."""
    with pytest.raises(HTTPException) as caught:
        # A DEPLOYED trainer, at a college this principal does not hold. The
        # undeployed carve-out deliberately does not apply here — that case has
        # its own test below.
        await _authorise_trainer(
            _DeployedTrainerSession(), _principal(persona), uuid.uuid4(), write=write
        )
    assert caught.value.status_code == 403


async def test_external_personas_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control that WORKS: `require_internal()` runs first, before any query.

    A trainer or college login is refused for every trainer id, so neither can use
    this endpoint as an id oracle. Locked in because the ordering — persona before
    existence — is what stops the 403/404 split from leaking the id space.
    """
    for persona in (Persona.TRAINER, Persona.COLLEGE):
        with pytest.raises(HTTPException) as caught:
            await _authorise_trainer(None, _principal(persona), uuid.uuid4(), write=False)
        assert caught.value.status_code == 403, persona


async def test_lde_executive_write_is_refused() -> None:
    """The control that WORKS: an LDE Executive may never push a trainer to ERM."""
    with pytest.raises(HTTPException) as caught:
        await _authorise_trainer(None, _principal(Persona.LDE_EXECUTIVE), uuid.uuid4(), write=True)
    assert caught.value.status_code == 403


def test_pipeline_personas_are_exactly_the_commercials_personas() -> None:
    """Documents the blast radius of SEC-01: which self-selectable personas hit this path."""
    assert _owns_trainer_pipeline(_principal(Persona.MANAGER)) is True
    assert _owns_trainer_pipeline(_principal(Persona.SENIOR_MANAGER)) is True
    assert _owns_trainer_pipeline(_principal(Persona.LDE_EXECUTIVE)) is False
    assert _owns_trainer_pipeline(_principal(Persona.TRAINER)) is False
    assert _owns_trainer_pipeline(_principal(Persona.COLLEGE)) is False
