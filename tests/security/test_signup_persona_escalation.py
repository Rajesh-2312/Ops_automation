"""SEC-01 — anyone on the internet may choose their own persona at signup.

`public.handle_new_user()` (0200_identity.sql:340-365) reads the persona out of
`new.raw_user_meta_data ->> 'role'` and writes it straight into `profiles.role`.
`raw_user_meta_data` is whatever the client put in the `data` object of
`POST /auth/v1/signup`, which needs nothing but the public anon key.

0200's own header already knows this is attacker-controlled ground:

    is_admin is never taken from user-supplied metadata — raw_user_meta_data is
    attacker-controlled at signup, and honouring `{"is_admin": true}` there would
    be the whole ballgame.

`is_admin` is indeed refused. `role` is not, and `role` is what
`can_see_commercials()`, `is_internal()` and `is_senior_manager()` are computed
from — which makes it the same ballgame by a different door.

Two settings turn this from a schema smell into a live door:
  * `GET /auth/v1/settings` reports `"disable_signup": false`;
  * `auto_confirm_email_on_signup` (1100) stamps `email_confirmed_at` on INSERT,
    so the account is usable immediately with no mailbox.
"""

from __future__ import annotations

import pytest

from tests.security.conftest import scalar

pytestmark = [pytest.mark.rls, pytest.mark.integration]


# SEC-01 CLOSED by migration 2100 (applied 2026-08-19). `handle_new_user` no
# longer reads raw_user_meta_data->>'role' at all; every signup lands on the
# deny-by-default 'trainer' sentinel and the persona is set only by an admin
# through the guarded UPDATE path in 0200.
#
# The xfail(strict=True) that used to sit here is deliberately DELETED rather
# than flipped to a skip: strict xfail is what turned the fix into a loud
# failure the moment it landed, which is the whole reason it was written that
# way. From here this is an ordinary regression test — if someone reinstates
# self-asserted roles, it fails.
@pytest.mark.parametrize("requested", ["senior_manager", "manager", "lde_executive", "college"])
def test_signup_metadata_cannot_choose_a_persona(as_new_signup, requested: str) -> None:
    """A self-asserted role must land on the deny-by-default sentinel, not be honoured."""
    with as_new_signup({"role": requested, "full_name": "Attacker"}) as (cur, user_id):
        role = scalar(cur, "select role from public.profiles where id = %s", (user_id,))
        assert role == "trainer", (
            f"signup metadata {{'role': {requested!r}}} produced profiles.role={role!r}. "
            "CLAUDE.md §4 binds persona to an admin act; this binds it to a form field."
        )


# SEC-01 CLOSED by 2100. A fresh signup is the 'trainer' sentinel, which since
# 1800 matches no policy anywhere, so it is outside the commercials wall by
# construction rather than by scope.
def test_fresh_signup_is_outside_the_commercials_wall(as_new_signup) -> None:
    """`can_see_commercials()` must be false for an account nobody has approved."""
    with as_new_signup({"role": "manager"}) as (cur, _):
        assert scalar(cur, "select public.can_see_commercials()") is False


def test_signup_metadata_cannot_set_is_admin(as_new_signup) -> None:
    """The defence 0200 DID implement, locked in. `is_admin` stays false.

    Unmarked: this control holds today and must keep holding. It is the reason
    SEC-01 stops short of handing out `is_admin()`, which governs the assignment
    tables — i.e. the ability to grant reach to oneself.
    """
    with as_new_signup({"role": "senior_manager", "is_admin": True}) as (cur, user_id):
        assert (
            scalar(cur, "select is_admin from public.profiles where id = %s", (user_id,)) is False
        )
        assert scalar(cur, "select public.is_admin()") is False


def test_unrecognised_role_falls_back_to_the_sentinel(as_new_signup) -> None:
    """A malformed or absent role resolves to `trainer`, which holds no policy (§4)."""
    for metadata in ({"role": "not_a_role"}, {}, {"role": "admin"}):
        with as_new_signup(metadata) as (cur, user_id):
            role = scalar(cur, "select role from public.profiles where id = %s", (user_id,))
            assert role == "trainer", f"metadata {metadata!r} produced role={role!r}"


def test_signup_is_auto_confirmed(as_new_signup) -> None:
    """Documents the second half of the chain: no mailbox is needed (migration 1100).

    Not a defect on its own — it is a deliberate decision recorded in
    `1100_no_email_confirmation.sql` — but it is why SEC-01 needs no email
    round-trip, so it is asserted here rather than left implicit.
    """
    with as_new_signup({"role": "manager"}) as (cur, user_id):
        # `authenticated` holds no grant on auth.users, so step back to the
        # session role to read it. Still inside the transaction that gets rolled back.
        cur.execute("reset role")
        confirmed = scalar(
            cur, "select email_confirmed_at is not null from auth.users where id = %s", (user_id,)
        )
        assert confirmed is True
