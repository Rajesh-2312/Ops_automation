"""CORS, and the response headers a cross-origin console is allowed to read.

WHY THIS FILE EXISTS
====================
There was no CORS middleware in this application at all, and the console has
never been same-origin with it. Vite serves the console on :5173 while the API
serves :8000; in production a static host serves the console while the API runs
somewhere that can run a process. Every browser call to FastAPI was therefore
refused by the browser before it was sent.

What made it hard to see is what the failure looks like from the outside. A
refused cross-origin `fetch` rejects with a `TypeError`, which the frontend maps
onto "Could not reach the API at ..." — a message about a service being down,
shown for a service that was up and idle. Nothing appears in the API log,
because nothing arrives.

THE SECOND HALF IS AN R4 CONTROL
================================
A cross-origin response exposes six headers to JavaScript and hides every other
one, including any header the application sets itself. The sheet and report
endpoints put the artifact's state in `X-Artifact-State` and the validation
outcome in `X-Validation-Blocked` precisely so the console cannot present a
blocked draft as an approved artifact. Cross-origin, those headers are invisible
unless they are named in `Access-Control-Expose-Headers`.

`frontend/src/lib/payouts.ts` reads an absent header as blocked and DRAFT, so
the omission fails safe — but every download then reads as a blocked draft,
which is the feature not working. The drift test below is what keeps a newly
added header from being set by the API and never seen by the console.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import payouts, reports
from app.core.config import Settings
from app.main import EXPOSED_RESPONSE_HEADERS, create_app

ALLOWED = "https://rajesh-2312.github.io"
DEV = "http://localhost:5173"

BASE_ENV = {
    "supabase_url": "https://example.supabase.co",
    "supabase_anon_key": "anon",
    "supabase_service_role_key": "service",
    "database_url": "postgresql://postgres:pw@db.example.com:5432/postgres",
}


def _settings(**overrides: str) -> Settings:
    """Settings built in isolation from the developer's own `.env`.

    `_env_file=None` for the reason `tests/unit/test_llm.py` gives: without it a
    local `CORS_ALLOWED_ORIGINS=` decides whether these pass.
    """
    return Settings(_env_file=None, **BASE_ENV, **overrides)  # type: ignore[arg-type]


def _client(origins: str) -> TestClient:
    return TestClient(create_app(settings=_settings(cors_allowed_origins=origins)))


# =============================================================================
# The allow-list
# =============================================================================


def test_no_origins_configured_installs_no_middleware() -> None:
    """Empty is not "allow nothing badly" — it is a same-origin deployment.

    Behind a reverse proxy the console and the API share an origin and CORS is
    not involved. Installing the middleware anyway would add a header to every
    response for no reader.
    """
    app = create_app(settings=_settings())
    assert [m.cls.__name__ for m in app.user_middleware] == []


def test_a_configured_origin_installs_it() -> None:
    app = create_app(settings=_settings(cors_allowed_origins=ALLOWED))
    assert "CORSMiddleware" in [m.cls.__name__ for m in app.user_middleware]


def test_the_list_is_split_and_stripped() -> None:
    settings = _settings(cors_allowed_origins=f" {DEV} , {ALLOWED} ")
    assert settings.cors_origins == [DEV, ALLOWED]


def test_a_trailing_slash_is_dropped() -> None:
    """An `Origin` header never carries one, and Starlette matches exactly.

    Configuring `https://host/` would therefore match nothing while looking
    entirely correct in the environment file.
    """
    assert _settings(cors_allowed_origins=f"{ALLOWED}/").cors_origins == [ALLOWED]


def test_a_wildcard_is_refused_at_configuration_time() -> None:
    """Not honoured, not warned about — refused before the app is built.

    Every call here carries a bearer token in a header. A wildcard invites any
    page on the internet to send one.
    """
    with pytest.raises(ValueError, match=r"may not contain"):
        _settings(cors_allowed_origins="*")


def test_the_wildcard_message_shows_what_to_write_instead() -> None:
    with pytest.raises(ValueError) as raised:
        _settings(cors_allowed_origins="*")
    assert "http://localhost:5173" in str(raised.value)


# =============================================================================
# What the browser is told
# =============================================================================


def test_a_preflight_from_an_allowed_origin_permits_the_bearer_token() -> None:
    """The preflight is the whole request as far as the browser is concerned.

    `Authorization` missing from `allow-headers` fails every authenticated call
    while an unauthenticated health check keeps working — which reads as an auth
    bug rather than a CORS one.
    """
    response = _client(ALLOWED).options(
        "/copilot/ask",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed
    assert "content-type" in allowed


def test_an_origin_that_is_not_on_the_list_is_not_answered() -> None:
    """The absence of the header is the refusal. There is no 403 to look for."""
    response = _client(ALLOWED).options(
        "/copilot/ask",
        headers={
            "Origin": "https://not-ours.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers


def test_one_entry_does_not_shadow_the_other() -> None:
    """Dev and prod origins coexist; configuring both must serve both."""
    client = _client(f"{DEV},{ALLOWED}")
    for origin in (DEV, ALLOWED):
        response = client.options(
            "/copilot/ask",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert response.headers["access-control-allow-origin"] == origin


def test_an_actual_response_carries_the_exposed_header_list() -> None:
    """`/health` needs no auth, so this asserts the middleware and not a route."""
    response = _client(ALLOWED).get("/health", headers={"Origin": ALLOWED})

    raw = response.headers["access-control-expose-headers"]
    exposed = {header.strip().lower() for header in raw.split(",")}
    for header in EXPOSED_RESPONSE_HEADERS:
        assert header.lower() in exposed


# =============================================================================
# The R4 drift guard
# =============================================================================


def test_every_header_the_artifact_endpoints_set_is_exposed() -> None:
    """A header the API sets and the console cannot read is a silent downgrade.

    Discovered by introspection rather than listed here a second time: a new
    `*_HEADER` constant in either module fails this test on the commit that adds
    it, which is the only moment anyone is thinking about it.
    """
    exposed = {header.lower() for header in EXPOSED_RESPONSE_HEADERS}

    for module in (payouts, reports):
        for name in dir(module):
            if not name.endswith("_HEADER"):
                continue
            value = getattr(module, name)
            assert isinstance(value, str)
            assert value.lower() in exposed, (
                f"{module.__name__}.{name} = {value!r} is set by the API but not exposed "
                "cross-origin, so the console reads it as absent. Add it to "
                "app.main.EXPOSED_RESPONSE_HEADERS."
            )


def test_the_download_filename_is_exposed() -> None:
    """`Content-Disposition` is not custom, and is not exposed by default either.

    Without it a generated sheet downloads under a name the browser invents,
    which for a remuneration sheet means a file nobody can identify later.
    """
    assert "Content-Disposition" in EXPOSED_RESPONSE_HEADERS


def test_the_exposed_list_has_no_duplicates() -> None:
    """payouts and reports both define `X-Artifact-State`, on purpose."""
    assert len(EXPOSED_RESPONSE_HEADERS) == len(set(EXPOSED_RESPONSE_HEADERS))
