"""Shared plumbing for the API contract tests.

These tests perform real HTTP round trips through the real routers against the
real Supabase database. That is the point: every model in `frontend/src/lib/*.ts`
was written by READING `app/api/*.py`, and reading is what these tests exist to
check.

WHY THE LOOP FACTORY (do not remove it)
=======================================
`TestClient` drives the app through anyio, which on Windows builds a
`ProactorEventLoop`. psycopg's async driver refuses to run on one, and the
resulting failure is the partial shape `run_api.py` documents: `/health` answers
200 and every database route raises `psycopg.InterfaceError` as a 500. Without
this factory the whole suite would "fail", and it would be the harness failing.

anyio forwards `backend_options["loop_factory"]` to `asyncio.Runner`, so this is
the same selector loop `run_api.py` hands uvicorn. On Linux and macOS it is a
no-op.

SKIPPING RATHER THAN FAILING
============================
A missing `DATABASE_URL`, a missing `SUPABASE_JWT_SECRET` or an unreachable
database skips the module. A contract test that fails because CI has no network
teaches nobody anything, and a red suite that is red for an uninteresting reason
stops being read.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import selectors
import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _dotenv_value(name: str) -> str:
    """Read ONE key from the repo `.env` WITHOUT mutating `os.environ`.

    This was `load_dotenv(...)` at conftest import, which pushes the entire
    `.env` — production `DATABASE_URL`, service-role key, `OPENROUTER_API_KEY` —
    into the process for every test in every directory, because conftest import
    is global. `tests/unit/test_llm.py` asserts the platform boots with NO LLM
    configuration ("Phase 1 has no AI in it", CLAUDE.md §13) and started failing
    because this file had silently supplied the key it asserts is absent.

    The same mistake was made independently in `tests/rag_eval/conftest.py`, so
    the pattern had already been copied once before either was noticed. Reading
    one named key on demand keeps the credential where the caller asked for it.
    """
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


DSN = os.environ.get("DATABASE_URL") or _dotenv_value("DATABASE_URL")
JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET") or _dotenv_value("SUPABASE_JWT_SECRET")

pytestmark = pytest.mark.skipif(
    not DSN or not JWT_SECRET,
    reason="DATABASE_URL and SUPABASE_JWT_SECRET are required for the contract suite",
)


def pytest_configure(config: pytest.Config) -> None:
    """Register `contract` here rather than in pyproject.toml.

    The marker is local to this package and this package is owned by one
    workstream; registering it from the conftest keeps the root config
    untouched and still silences `PytestUnknownMarkWarning`.
    """
    config.addinivalue_line(
        "markers",
        "contract: real HTTP round trip against the live routers and database",
    )


def _loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


@dataclass(frozen=True)
class Fixtures:
    """Real ids, resolved once per session from the live database.

    Anchored on the §6 payout engagements — the ₹65,000 and ₹80,000 per-month
    work orders with July 2026 marks — and everything else derived from whichever
    program carries them, so persona reach and payout arithmetic cannot disagree
    about which college is under test.
    """

    college_id: str
    program_id: str
    deployment_65k: str
    deployment_80k: str
    manager_id: str
    senior_manager_id: str
    lde_id: str
    trainer_id: str
    program_out_of_reach: str
    artifact_type: str
    artifact_id: str

    missing_uuid: str = "00000000-0000-4000-8000-000000000000"


@pytest.fixture(scope="session")
def db_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(DSN, connect_timeout=15) as conn, conn.cursor() as cur:
            cur.execute("select 1")
    except Exception as exc:  # pragma: no cover - environment, not code
        pytest.skip(f"database unreachable: {type(exc).__name__}: {exc}")
    return True


@pytest.fixture(scope="session")
def fixtures(db_available: bool) -> Fixtures:
    import psycopg

    with psycopg.connect(DSN, connect_timeout=20) as conn, conn.cursor() as cur:
        # §6's fixtures are defined by their TERMS, so the query encodes the terms:
        # a per-month work order, and July 2026 marked end to end with nothing that
        # deducts. A deployment with 29 of 31 days marked also satisfies "roughly a
        # full month" and pays 60,806 — which is the correct answer to a different
        # question, and picking it would fail the fixture for a reason that is not
        # a bug. `not exists (A|H)` is what makes the choice deterministic.
        cur.execute("""
            select d.id, w.rate, b.program_id, p.college_id
            from deployments d
            join batches b on b.id = d.batch_id
            join programs p on p.id = b.program_id
            join work_orders w on w.trainer_id = d.trainer_id and w.program_id = b.program_id
            where w.rate_basis = 'per_month'
              and (d.start_date is null or d.start_date <= date '2026-07-01')
              and (d.end_date is null or d.end_date >= date '2026-07-31')
              and (select count(*) from trainer_attendance a
                    where a.deployment_id = d.id
                      and a.mark_date between date '2026-07-01' and date '2026-07-31') = 31
              and not exists (select 1 from trainer_attendance a
                    where a.deployment_id = d.id
                      and a.mark_date between date '2026-07-01' and date '2026-07-31'
                      and a.mark in ('A', 'H'))
            order by w.rate desc
            """)
        rows = cur.fetchall()

        def person(role: str, college: str) -> str:
            cur.execute(
                """
                select p.id from profiles p
                where p.role = %s
                  and (exists (select 1 from user_college_assignments u
                               where u.user_id = p.id and u.college_id = %s)
                    or exists (select 1 from user_cluster_assignments uc
                               join colleges c on c.cluster_id = uc.cluster_id
                               where uc.user_id = p.id and c.id = %s))
                limit 1
                """,
                (role, college, college),
            )
            row = cur.fetchone()
            return str(row[0]) if row else ""

        # A ₹65,000 engagement in a college NOBODY manages is a correct 403 for
        # every caller and would skip the whole suite. Walk the candidates and
        # take the first that a Manager can actually reach.
        dep65 = program_id = college_id = manager = ""
        for did, rate, prog, col in rows:
            if Decimal(rate) != Decimal("65000.00"):
                continue
            candidate = person("manager", str(col))
            if candidate:
                dep65, program_id, college_id, manager = str(did), str(prog), str(col), candidate
                break
        if not dep65:
            pytest.skip(
                "no bCAP 65,000/month July 2026 engagement is reachable by any Manager, "
                "so CLAUDE.md §6 fixture 2 cannot be driven through the API here"
            )

        dep80 = ""
        for did, rate, prog, _col in rows:
            if Decimal(rate) == Decimal("80000.00") and str(prog) == program_id:
                dep80 = str(did)
                break

        senior = person("senior_manager", college_id)
        lde = person("lde_executive", college_id)

        cur.execute(
            "select id from programs where college_id is distinct from %s limit 1", (college_id,)
        )
        row = cur.fetchone()
        out_of_reach = str(row[0]) if row else ""

        cur.execute(
            """
            select distinct d.trainer_id from deployments d
            join batches b on b.id = d.batch_id
            join programs p on p.id = b.program_id
            where p.college_id = %s limit 1
            """,
            (college_id,),
        )
        row = cur.fetchone()
        trainer_id = str(row[0]) if row else ""

        cur.execute("select artifact_type, artifact_id from artifact_versions limit 1")
        row = cur.fetchone()
        artifact_type = str(row[0]) if row else "remuneration_sheets"
        artifact_id = str(row[1]) if row else Fixtures.missing_uuid

    if not manager:
        pytest.skip("no manager profile reaches the fixture college")

    return Fixtures(
        college_id=college_id,
        program_id=program_id,
        deployment_65k=dep65,
        deployment_80k=dep80,
        manager_id=manager,
        senior_manager_id=senior,
        lde_id=lde,
        trainer_id=trainer_id,
        program_out_of_reach=out_of_reach,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
    )


def mint(user_id: str, *, ttl: int = 3600, audience: str = "authenticated") -> str:
    """An HS256 token of the shape `app.core.security` accepts.

    Supabase issues ES256 against JWKS on a migrated project and HS256 against
    the shared secret otherwise; `verify_access_token` picks by header and
    accepts both, and HS256 is the branch a test can produce without a browser.
    """
    from jose import jwt

    return jwt.encode(
        {
            "sub": user_id,
            "aud": audience,
            "role": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl,
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint(user_id)}"}


def _drop_cached_engine() -> None:
    """Forget `app.db.session`'s process-wide engine so it is rebuilt on OUR loop.

    `get_engine()` is an `lru_cache(maxsize=1)`, and an async engine's pooled
    connections belong to the event loop that opened them. This suite runs the
    whole app on the private selector loop `_loop_factory` builds, so if anything
    earlier in the pytest session had already warmed that cache on a different
    loop, every database route here would fail with `InterfaceError` — the exact
    partial shape `run_api.py` documents, arriving as a wall of 500s that look
    like an application fault rather than a harness one.

    Nothing in the suite warms it today (`tests/unit/test_api_*.py` import
    `get_session` only to override the dependency, so they never reach the
    engine). This makes that a property of the harness rather than a property of
    what happens to have run first. It is the same clearing `dispose_engine()`
    already does when the `TestClient` context exits below, moved to the other
    end so the invariant holds on entry too.
    """
    from app.db import session as db_session

    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()


@pytest.fixture(scope="package")
def client(db_available: bool) -> Iterator[Any]:
    """The real app, on a connection pool that dies with THIS package.

    PACKAGE-SCOPED, NOT SESSION-SCOPED, AND THAT IS A DATABASE DECISION
    ==================================================================
    Entering the `TestClient` context starts the app; leaving it runs the
    lifespan shutdown, which calls `dispose_engine()` and closes every pooled
    connection. Under `scope="session"` that exit happens at the END OF THE
    WHOLE PYTEST RUN — so in `pytest tests/api_contract tests/security` this
    suite's connection pool is still open, still holding whatever the server
    last held, for the entire security suite that follows.

    That is not theoretical. `tests/security/test_truncate_grant.py` issues
    `truncate table public.trainer_attendance cascade`, which needs ACCESS
    EXCLUSIVE on that table and on everything CASCADE reaches. Anything still
    holding a lock there does not make it fail — it makes it WAIT, and a suite
    that waits is a suite whose result depends on what else is in the run.

    Package scope keeps the fixture built exactly once for this directory (the
    cost that made it session-scoped in the first place) while ending it at this
    directory's last test instead of the session's.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    _drop_cached_engine()
    with TestClient(
        create_app(),
        backend_options={"loop_factory": _loop_factory},
        # A 500 must arrive as a 500 so a test can assert on it, rather than
        # being re-raised into the test as the original exception.
        raise_server_exceptions=False,
    ) as test_client:
        yield test_client


# Package-scoped because it depends on `client`, and a wider scope may not
# depend on a narrower one.
@pytest.fixture(scope="package")
def openapi(client: Any) -> dict[str, Any]:
    from app.main import create_app

    return create_app().openapi()


@pytest.fixture
def cleanup() -> Iterator[list[tuple[str, str]]]:
    """Rows a test created, deleted by primary key when it finishes.

    Append `(table, id)`. Nothing else is ever removed — a test must not delete a
    row it did not create, and the audit trail is append-only by design (§11), so
    audit rows raised as a side effect of exercising an endpoint are left alone
    and reported rather than tidied away.
    """
    created: list[tuple[str, str]] = []
    yield created
    if not created:
        return
    import psycopg

    with psycopg.connect(DSN, connect_timeout=20) as conn, conn.cursor() as cur:
        for table, row_id in reversed(created):
            cur.execute(f"delete from {table} where id = %s", (row_id,))
        conn.commit()


JULY = ("2026-07-01", "2026-07-31")
TODAY = dt.date(2026, 7, 1)
