"""The persona guards. CLAUDE.md R5 — boundaries are tested, not assumed.

These are the app-side half of the RLS test suite. `supabase/tests/` proves an
LDE Executive gets zero rows from `pnl` through a policy; this file proves the
same persona is refused by `require_commercials()` in code — which is the check
that actually runs, because FastAPI connects with a `BYPASSRLS` credential and
the policy never fires for us at all.

Every test here is database-free by construction: `Principal` is a plain frozen
dataclass and each guard is a plain function over it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi import HTTPException
from jose import jwt

from app.core.security import (
    JWT_AUDIENCE,
    Principal,
    decode_supabase_jwt,
    require_college_reach,
    require_commercials,
    require_internal,
    require_persona,
)
from app.domain.enums import COMMERCIALS_PERSONAS, INTERNAL_PERSONAS, Persona

SECRET = "test-jwt-secret-not-a-real-one"

COLLEGE_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
COLLEGE_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def principal(
    persona: Persona,
    *,
    colleges: frozenset[uuid.UUID] = frozenset({COLLEGE_A}),
    is_admin: bool = False,
) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        persona=persona,
        college_ids=colleges,
        is_admin=is_admin,
    )


# --- the commercials wall (CLAUDE.md §4, R5) ---------------------------------


def test_lde_executive_is_refused_commercials():
    """THE test. §4: "LDE Executive [...] No commercials." """
    with pytest.raises(HTTPException) as exc:
        require_commercials(principal(Persona.LDE_EXECUTIVE))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("persona", sorted(COMMERCIALS_PERSONAS))
def test_commercials_personas_pass(persona):
    assert require_commercials(principal(persona)) is None


@pytest.mark.parametrize(
    "persona",
    sorted(set(Persona) - COMMERCIALS_PERSONAS),
)
def test_every_other_persona_is_refused_commercials(persona):
    """Trainer and College too, not only the LDE Executive."""
    with pytest.raises(HTTPException) as exc:
        require_commercials(principal(persona))
    assert exc.value.status_code == 403


def test_commercials_set_is_not_re_derived():
    """The wall is one definition. If this drifts, `security.py` forked it."""
    assert frozenset({Persona.SENIOR_MANAGER, Persona.MANAGER}) == COMMERCIALS_PERSONAS
    for persona in Persona:
        assert principal(persona).can_see_commercials == (persona in COMMERCIALS_PERSONAS)


def test_commercials_is_the_wall_not_the_scope():
    """A Manager passes the wall even for a college they cannot reach.

    Which is exactly why every money policy is a two-conjunct predicate and every
    commercial endpoint must call BOTH guards. This test pins the trap open so
    nobody "fixes" `require_commercials` into doing reach as well and leaves the
    call sites believing one call is enough.
    """
    manager = principal(Persona.MANAGER, colleges=frozenset({COLLEGE_A}))
    assert require_commercials(manager) is None
    with pytest.raises(HTTPException):
        require_college_reach(manager, COLLEGE_B)


# --- reach: mirror of can_reach_college() ------------------------------------


def test_reach_allows_an_assigned_college():
    assert require_college_reach(principal(Persona.MANAGER), COLLEGE_A) is None


def test_reach_denies_an_unassigned_college():
    with pytest.raises(HTTPException) as exc:
        require_college_reach(principal(Persona.MANAGER), COLLEGE_B)
    assert exc.value.status_code == 403


def test_no_assignments_reaches_nothing():
    """Deny by default: a freshly created staff account sees zero rows."""
    fresh = principal(Persona.MANAGER, colleges=frozenset())
    with pytest.raises(HTTPException):
        require_college_reach(fresh, COLLEGE_A)


def test_admin_is_not_reach():
    """`is_admin()` is the right to HAND OUT reach; it is not itself reach.

    `0700_finance.sql`: "A Senior Manager with no cluster assignment sees no
    P&L, and that is correct." No admin override anywhere in this module.
    """
    admin = principal(Persona.SENIOR_MANAGER, colleges=frozenset(), is_admin=True)
    with pytest.raises(HTTPException) as exc:
        require_college_reach(admin, COLLEGE_A)
    assert exc.value.status_code == 403


# --- persona guards ----------------------------------------------------------


@pytest.mark.parametrize("persona", sorted(INTERNAL_PERSONAS))
def test_internal_personas_pass_require_internal(persona):
    assert require_internal(principal(persona)) is None
    assert principal(persona).is_internal


@pytest.mark.parametrize("persona", [Persona.TRAINER, Persona.COLLEGE])
def test_external_personas_are_refused_require_internal(persona):
    with pytest.raises(HTTPException) as exc:
        require_internal(principal(persona))
    assert exc.value.status_code == 403
    assert not principal(persona).is_internal


def test_require_persona_allows_listed():
    guard = require_persona(Persona.SENIOR_MANAGER, Persona.MANAGER)
    caller = principal(Persona.MANAGER)
    assert guard(caller) is caller


def test_require_persona_denies_unlisted():
    guard = require_persona(Persona.SENIOR_MANAGER, Persona.MANAGER)
    with pytest.raises(HTTPException) as exc:
        guard(principal(Persona.LDE_EXECUTIVE))
    assert exc.value.status_code == 403


def test_require_persona_with_no_personas_is_a_programming_error():
    """An empty guard must not mean "allow everyone" — it means "you wrote a bug"."""
    with pytest.raises(ValueError, match="at least one persona"):
        require_persona()


def test_principal_is_frozen():
    """A handler must not be able to widen its own caller's scope mid-request."""
    caller = principal(Persona.LDE_EXECUTIVE)
    with pytest.raises((AttributeError, TypeError)):
        caller.persona = Persona.SENIOR_MANAGER  # type: ignore[misc]


# --- token verification ------------------------------------------------------


def make_token(**overrides) -> str:
    claims = {
        "sub": str(uuid.uuid4()),
        "aud": JWT_AUDIENCE,
        "role": "authenticated",
        "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_valid_token_decodes_to_its_subject():
    user_id = uuid.uuid4()
    claims = decode_supabase_jwt(make_token(sub=str(user_id)), secret=SECRET)
    assert claims.sub == user_id


def test_token_signed_with_another_secret_is_rejected():
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "aud": JWT_AUDIENCE}, "attacker-secret", algorithm="HS256"
    )
    with pytest.raises(HTTPException) as exc:
        decode_supabase_jwt(forged, secret=SECRET)
    assert exc.value.status_code == 401


def test_expired_token_is_rejected():
    past = int((dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).timestamp())
    with pytest.raises(HTTPException) as exc:
        decode_supabase_jwt(make_token(exp=past), secret=SECRET)
    assert exc.value.status_code == 401


def test_wrong_audience_is_rejected():
    with pytest.raises(HTTPException) as exc:
        decode_supabase_jwt(make_token(aud="anon"), secret=SECRET)
    assert exc.value.status_code == 401


def test_garbage_is_rejected():
    with pytest.raises(HTTPException) as exc:
        decode_supabase_jwt("not.a.jwt", secret=SECRET)
    assert exc.value.status_code == 401


def test_persona_is_never_taken_from_the_token():
    """A JWT keeps asserting whatever it was minted with, for its whole lifetime.

    So a token claiming `senior_manager` must not produce a senior-manager
    `Principal`. `JwtClaims` has no persona field at all — `role` is the Postgres
    role — and persona is read from `profiles` on every request instead.
    """
    claims = decode_supabase_jwt(
        make_token(role="senior_manager", app_role="senior_manager", is_admin=True),
        secret=SECRET,
    )
    assert not hasattr(claims, "persona")
    assert not hasattr(claims, "is_admin")
    # `role` survives only as the Postgres role, and nothing reads it for authz.
    assert claims.role == "senior_manager"
