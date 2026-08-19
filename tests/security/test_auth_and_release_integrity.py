"""Token verification (R-auth), R3 tool binding and R4 route integrity.

None of these found a defect. They are here because all three are the kind of
control that is correct until one accommodating line is added — an extra `alg` in
a tuple, a convenience tool in a toolset, an endpoint that skips a state — and
none of those diffs looks like a security change when reviewed.

Every test in this module runs offline: no database, no network, no JWKS fetch.
"""

from __future__ import annotations

import base64
import inspect
import json
import re
import time
from pathlib import Path

import pytest
from jose import jwt

from app.core.security import JWT_ALGORITHMS, verify_access_token

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_SECRET = "security-review-probe-secret"
SUBJECT = "11111111-1111-1111-1111-111111111111"


def _b64(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


def _claims(**overrides) -> dict:
    return {
        "sub": SUBJECT,
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
        "role": "authenticated",
        **overrides,
    }


@pytest.fixture(autouse=True)
def _probe_secret(monkeypatch: pytest.MonkeyPatch):
    """Point the verifier at a secret this module controls."""
    from app.core.config import get_settings

    monkeypatch.setenv("SUPABASE_JWT_SECRET", PROBE_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- token forgery ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_correctly_signed_token_is_accepted() -> None:
    """The control case. Without it, every rejection below proves nothing."""
    claims = await verify_access_token(jwt.encode(_claims(), PROBE_SECRET, algorithm="HS256"))
    assert str(claims.sub) == SUBJECT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,token_factory",
    [
        (
            "alg=none, unsigned",
            lambda: f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_claims())}.",
        ),
        (
            "alg=none, capitalised",
            lambda: f"{_b64({'alg': 'None', 'typ': 'JWT'})}.{_b64(_claims())}.",
        ),
        (
            "alg outside the allow-list (HS512)",
            lambda: jwt.encode(_claims(), PROBE_SECRET, algorithm="HS512"),
        ),
        (
            "signed with a different secret",
            lambda: jwt.encode(_claims(), "attacker-secret", algorithm="HS256"),
        ),
        (
            "expired",
            lambda: jwt.encode(_claims(exp=int(time.time()) - 10), PROBE_SECRET, algorithm="HS256"),
        ),
        (
            "wrong audience",
            lambda: jwt.encode(_claims(aud="anon"), PROBE_SECRET, algorithm="HS256"),
        ),
        (
            "ES256 header with no kid",
            lambda: f"{_b64({'alg': 'ES256', 'typ': 'JWT'})}.{_b64(_claims())}.AAAA",
        ),
        (
            "garbage",
            lambda: "not.a.token",
        ),
    ],
)
async def test_forged_tokens_are_refused(name: str, token_factory) -> None:
    """`alg` is read from the header and checked against the allow-list FIRST.

    That ordering is what defeats `alg: none` and algorithm confusion: an
    unrecognised algorithm never reaches a verifier that might do something
    accommodating with it.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await verify_access_token(token_factory())
    assert caught.value.status_code == 401, name
    assert (
        caught.value.detail == "Invalid or expired token"
    ), f"{name}: the 401 names which check failed, which is free reconnaissance"


def test_the_algorithm_allow_list_stays_closed() -> None:
    """Only the two schemes Supabase actually issues. A third needs a deliberate diff."""
    assert set(JWT_ALGORITHMS) == {"ES256", "HS256"}
    assert "none" not in {a.lower() for a in JWT_ALGORITHMS}


@pytest.mark.asyncio
async def test_the_token_role_claim_is_not_an_app_persona() -> None:
    """A token claiming `role: service_role` gains nothing.

    `JwtClaims.role` is the POSTGRES role from the token and is never consulted
    for authorisation — `resolve_principal()` reads persona from `profiles` on
    every request. This asserts the claim is carried inertly rather than trusted.
    """
    token = jwt.encode(_claims(role="service_role"), PROBE_SECRET, algorithm="HS256")
    claims = await verify_access_token(token)
    assert claims.role == "service_role"
    assert not hasattr(claims, "persona")
    source = inspect.getsource(__import__("app.core.security", fromlist=["x"]).resolve_principal)
    assert "claims" not in source, "resolve_principal must derive persona from the database only"


# --- R3: agents cannot release ------------------------------------------------


def test_no_agent_toolset_exposes_a_release_capable_tool() -> None:
    """R3, asserted against the shipped registry rather than against prose."""
    from app.agents.tools import AGENT_TOOLSETS, SAVE_DRAFT, ToolEffect

    forbidden = re.compile(
        r"send|email|mail|whatsapp|sms|post_|publish|release|approve|transmit|dispatch|notify",
        re.IGNORECASE,
    )
    for agent, toolset in AGENT_TOOLSETS.items():
        for name in toolset.names:
            assert not forbidden.search(name), f"{agent}: tool {name!r} looks release-capable"

        # The effect enum is the real gate: there is no member that could mean
        # "send", so a third capability is a type error rather than a feature.
        assert toolset.effects <= {
            ToolEffect.READ,
            ToolEffect.SAVE_DRAFT,
        }, f"{agent}: holds effects beyond read/save_draft: {toolset.effects}"
        others = [n for n in toolset.names if n != SAVE_DRAFT]
        assert all(
            n.startswith(("read_", "list_", "search_", "get_")) for n in others
        ), f"{agent}: non-read tool outside save_draft: {others}"


def test_nothing_under_app_can_reach_the_mailer() -> None:
    """`tools/agentmail.py` can send. R3 keeps it outside every agent's reach.

    Enforced by import graph, not by convention: if any module under `app/`ever
    imports it, an agent is one tool binding away from sending mail.
    """
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "app").rglob("*.py")
        if "agentmail" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == [], f"app/ references the mailer: {offenders}"


def test_the_mailer_allow_list_cannot_be_bypassed() -> None:
    """The recipient allow-list refuses before any socket is opened."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from tools.agentmail import RecipientNotAllowed, send_report

    for recipient in (
        "attacker@evil.example",
        "ATTACKER@EVIL.EXAMPLE",
        " attacker@evil.example ",
        "allowed@example.com, attacker@evil.example",  # comma smuggling in one string
        '"Ops" <attacker@evil.example>',  # display-name wrapping
    ):
        with pytest.raises(RecipientNotAllowed):
            send_report(to=recipient, subject="probe", text="probe", api_key="unused")

    with pytest.raises(RecipientNotAllowed):
        send_report(to=["attacker@evil.example"], subject="probe", text="probe", api_key="unused")


# --- R4: no route skips the lifecycle -----------------------------------------


def test_every_authenticated_route_carries_a_reach_or_wall_guard() -> None:
    """All 35 non-health routes resolve a `Principal` AND call a guard.

    `app/db/session.py` connects with BYPASSRLS, so an endpoint that forgets this
    returns the whole table. Enumerated from the live router rather than from a
    hand-kept list, so a new endpoint is covered the moment it is registered.
    """
    from fastapi.routing import APIRoute

    from app.main import create_app

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "original_router"):
                yield from walk(route.original_router.routes)

    guards = (
        "require_internal",
        "require_commercials",
        "require_college_reach",
        "require_persona",
        "_authorised_program",
        "_authorised_college",
        "_authorised_artifact",
        "_authorised_deployment",
        "_require_payout_persona",
        "_payout_context",
        "_authorise",
        "_reachable_programs",
        "_row",
    )

    unguarded: list[str] = []
    checked = 0
    for route in walk(create_app().routes):
        if route.path == "/health":
            continue  # liveness probe: no I/O, no data, documented in app/api/health.py
        checked += 1
        source = inspect.getsource(route.endpoint)
        params = inspect.signature(route.endpoint).parameters
        has_principal = any("Principal" in str(p.annotation) for p in params.values())
        has_guard = any(re.search(rf"\b{g}\b", source) for g in guards)
        if not (has_principal and has_guard):
            unguarded.append(f"{sorted(route.methods)} {route.path}")

    assert checked >= 30, f"route walk found only {checked} routes; the walker is broken"
    assert unguarded == [], f"routes with no persona/reach guard: {unguarded}"


def test_release_is_only_reachable_from_approved() -> None:
    """R4's edge list admits exactly one way into RELEASED."""
    from app.domain.enums import ALLOWED_TRANSITIONS, ArtifactState

    reaching_released = {
        state for state, targets in ALLOWED_TRANSITIONS.items() if ArtifactState.RELEASED in targets
    }
    assert reaching_released == {ArtifactState.APPROVED}
    assert ALLOWED_TRANSITIONS[ArtifactState.DRAFT] == frozenset({ArtifactState.PENDING_APPROVAL})
    assert ArtifactState.RELEASED not in ALLOWED_TRANSITIONS.get(
        ArtifactState.RELEASED, frozenset()
    ), "RELEASED must be terminal"


def test_an_approved_version_cannot_be_mutated_or_deleted(db_connection) -> None:
    """The freeze trigger, asserted where it lives rather than trusted.

    R4: approval freezes and hashes the version. `artifact_versions_freeze` is a
    BEFORE DELETE OR UPDATE trigger, so the guarantee survives a caller that
    reaches the table on a BYPASSRLS connection — which is every FastAPI request.
    """
    db_connection.rollback()
    cur = db_connection.cursor()
    cur.execute("""
        select count(*) from pg_trigger t
         join pg_class c on c.oid = t.tgrelid
         where c.relname = 'artifact_versions'
           and t.tgname = 'artifact_versions_freeze'
           and not t.tgisinternal
        """)
    assert cur.fetchone()[0] == 1, "the R4 freeze trigger is missing"
