"""Database harness for the security suite.

SAFETY CONTRACT — READ BEFORE ADDING A TEST HERE
================================================
These tests write. They insert `auth.users` rows to exercise the signup trigger,
they UPDATE payment rails to prove a policy is too wide, and two of them TRUNCATE
a table to prove RLS does not cover that verb.

**Nothing is ever committed.** Every fixture below hands out a cursor on a
connection with `autocommit = False` and rolls it back in teardown, so the
database is byte-identical after the run. There is no `commit()` anywhere in this
package and none may be added: a test that commits here would be indistinguishable
from the attack it is describing.

The other half of the contract is impersonation. `postgres` — the role
`DATABASE_URL` connects as — carries BYPASSRLS, so a query issued straight down
this connection proves nothing about RLS. `as_user()` therefore sets
`request.jwt.claims` (which is what `auth.uid()` reads) and then `set local role
authenticated`, which is exactly the pair PostgREST establishes for a browser
session. The rollback resets both, so no probe can leak persona into the next.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- the demo estate these tests probe against --------------------------------
#
# Two tenants, which is the whole point: a finding that only shows up as "user X
# sees their own rows" is not a finding. `tools/demo_data.py` seeds both.
#
#   Tenant A  Demo Institute of Technology   (in cluster "Demo Cluster - South")
#   Tenant B  Malineni Lakshmaiah Women's engineering college (no cluster)

USERS = {
    # senior_manager + is_admin, assigned to Malineni
    "sm_admin_malineni": "a07dd66e-f792-490d-88f7-6eedf8d8326d",
    # senior_manager, cluster South -> reaches Demo Institute ONLY
    "sm_cluster_south": "89d9d7ab-42ee-527e-aac9-e276b0fb80c8",
    # senior_manager with NO assignment of any kind -> reaches nothing
    "sm_no_reach": "7b594a10-d901-5498-8b05-c0f8784dbcdc",
    # manager -> Demo Institute
    "mgr_demo_inst": "7108d27e-1242-5337-972a-8d8c57998ad9",
    # manager with NO assignment of any kind -> reaches nothing
    "mgr_no_reach": "569300e5-6b1e-5244-911e-ecf14ca1052d",
    # lde_executive -> Demo Institute
    "lde_demo_inst": "2266f091-e66a-5c0e-856c-d7c6234c9c23",
    # lde_executive -> Malineni
    "lde_malineni": "06c7749d-0dec-4b0e-a2a4-70a2ce251d46",
}

#: The persona each subject must hold for a probe against it to mean anything.
#:
#: Migration 2100 demoted every unprovisioned internal profile to the 'trainer'
#: sentinel — correctly, since an internal persona with zero assignments is
#: indistinguishable from an account created through the SEC-01 hole it closed.
#: `sm_no_reach` and `mgr_no_reach` are unprovisioned BY DESIGN — that is the
#: whole point of them — so the fix demoted exactly the two subjects this suite
#: needs, and every probe using them silently became a probe of the trainer
#: sentinel instead.
#:
#: Rather than re-seed two permanently-unprovisioned Managers into the live
#: schema (the exact shape SEC-02/03 makes dangerous — a test suite should not
#: be the reason one exists), `as_user` restores the intended persona inside the
#: rolled-back transaction. Nothing persists.
EXPECTED_PERSONA = {
    "sm_admin_malineni": "senior_manager",
    "sm_cluster_south": "senior_manager",
    "sm_no_reach": "senior_manager",
    "mgr_demo_inst": "manager",
    "mgr_no_reach": "manager",
    "lde_demo_inst": "lde_executive",
    "lde_malineni": "lde_executive",
}

#: Reverse lookup, so `as_user(USERS[key])` provisions without the caller
#: repeating the persona at every call site.
PERSONA_BY_ID = {USERS[key]: persona for key, persona in EXPECTED_PERSONA.items()}

COLLEGE_MALINENI = "1641c9c0-1d30-4b0f-b7c0-3c9a86419a6d"
COLLEGE_DEMO_INSTITUTE = "eb8ca94c-f0b6-5292-b330-1a48e3cbe581"

#: A trainer engaged at Malineni (tenant B). Nobody scoped to tenant A reaches him.
TRAINER_VEMA = "e2d183e9-320f-4cd9-b055-7f593ac13af3"
#: A trainer at Demo Institute (tenant A) who HAS a `trainer_bank_accounts` row.
TRAINER_ANITHA = "bf1a1f79-b7a7-5d3e-9178-b2f6250ed3dc"

#: Tables whose whole reason to exist is that an LDE Executive never sees them.
COMMERCIAL_TABLES = (
    "pnl",
    "remuneration_sheets",
    "work_orders",
    "trainer_bank_accounts",
)


def _database_url() -> str | None:
    """`DATABASE_URL` from the environment, falling back to the repo `.env`.

    Read directly rather than through `app.core.config` so this package has no
    import-time dependency on application settings — the suite must be able to
    report on a service it cannot boot.
    """
    import os

    value = os.environ.get("DATABASE_URL")
    if value:
        return value
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL=") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


#: The live connection, while `db_connection` is set up. A list rather than a
#: module global so `_no_transaction_outlives_a_test` can be a no-op — not an
#: error — in the run where the fixture skipped and there is no connection.
_OPEN_CONNECTIONS: list[Any] = []


@pytest.fixture(scope="package")
def db_connection() -> Iterator[Any]:
    """One non-autocommit connection for this package, rolled back and closed at its end.

    Shared rather than per-test because each probe is a sub-second query and the
    database is remote: opening a connection per test turned a 6-second suite
    into a 4-minute one during the review.

    PACKAGE, not session. The saving is identical — the connection is still
    opened once and reused by every test here — but it is CLOSED when this
    directory's last test finishes rather than when the whole pytest run
    finishes. A suite that keeps a database connection alive while other suites
    run is a suite whose neighbours' results depend on it, and this one writes:
    it inserts `auth.users` rows, UPDATEs payment rails and TRUNCATEs a table,
    all inside a transaction that is rolled back but whose locks are real until
    it ends.
    """
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL is not configured; RLS probes need a live database")
    psycopg = pytest.importorskip("psycopg", reason="psycopg is needed for RLS probes")

    connection = psycopg.connect(url, connect_timeout=20)
    connection.autocommit = False
    _OPEN_CONNECTIONS.append(connection)
    try:
        yield connection
    finally:
        _OPEN_CONNECTIONS.clear()
        connection.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def _no_transaction_outlives_a_test() -> Iterator[None]:
    """End this suite's transaction after every test, not merely at session end.

    `db_connection` is SESSION-scoped and never commits, which is the safety
    contract at the top of this file. The gap that leaves is TIME. A test that
    writes and does not roll back explicitly leaves the connection `idle in
    transaction` until the whole pytest session ends — and `as_user` /
    `as_new_signup` roll back in a `finally`, but
    `tests/security/test_truncate_grant.py` drives `db_connection.cursor()`
    directly and one of its probes issues `create table`.

    Harmless when this package runs alone, which is why it went unnoticed. Once
    it shares a run:

    * the locks that transaction holds are held for the REST OF THE SESSION, so
      whatever runs next — `tests/api_contract` drives real writes through the
      real routers — can block on them instead of failing or passing on its own
      merits, and "which suite ran first" starts deciding results;
    * Postgres' `idle_in_transaction_session_timeout` can reap the connection
      mid-run, at which point every remaining probe here fails for a reason that
      has nothing to do with RLS.

    Rolling back an idle connection with no open transaction is a no-op, so this
    costs nothing on the tests that were already careful.

    A connection that has already died takes the failing test's report with it if
    this raises during teardown — one real failure would be reported as a failure
    AND an error, and the error would name this fixture rather than the cause. So
    a dead connection is left for the test itself to report.
    """
    yield
    for connection in _OPEN_CONNECTIONS:
        if connection.closed:
            continue
        with suppress(Exception):
            connection.rollback()


@pytest.fixture
def as_user(db_connection: Any):
    """Factory yielding a cursor that acts as one user, under RLS. Always rolls back.

    Usage::

        with as_user(USERS["mgr_no_reach"]) as cur:
            cur.execute("select count(*) from public.trainer_bank_accounts")
    """

    @contextmanager
    def _impersonate(
        user_id: str | None,
        role: str = "authenticated",
        ensure_persona: str | None = None,
    ) -> Iterator[Any]:
        db_connection.rollback()
        cursor = db_connection.cursor()
        try:
            # `ensure_persona` sets the subject's persona INSIDE this
            # rolled-back transaction, before impersonation starts.
            #
            # It exists because migration 2100 demoted every unprovisioned
            # internal profile to the 'trainer' sentinel — correctly; an
            # internal persona with no assignments is indistinguishable from an
            # account created through the SEC-01 hole. Two of those profiles
            # were what `mgr_no_reach` and `sm_no_reach` pointed at, so the
            # suite's "internal persona that reaches nothing" subjects stopped
            # being internal the moment the fix landed. The drift guard in
            # `_check` caught it, which is what it is for.
            #
            # Provisioning here rather than re-seeding the database is
            # deliberate: a permanently-unprovisioned Manager sitting in the
            # live schema is the exact shape SEC-02/03 makes dangerous, and a
            # test suite should not be the reason one exists. This runs as the
            # superuser connection, where `auth.uid()` is NULL and
            # `profiles_guard_privileged_columns` returns early by design.
            persona = ensure_persona or PERSONA_BY_ID.get(user_id or "")
            if persona is not None and user_id:
                cursor.execute(
                    "update public.profiles set role = %s::public.app_role where id = %s",
                    (persona, user_id),
                )
            claims: dict[str, str] = {"role": role, "aud": "authenticated"}
            if user_id:
                claims["sub"] = user_id
            cursor.execute(
                "select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),)
            )
            cursor.execute(f"set local role {role}")
            yield cursor
        finally:
            # Never commit. This also resets `role` and the claims GUC.
            db_connection.rollback()

    return _impersonate


@pytest.fixture
def as_new_signup(db_connection: Any):
    """Factory that performs a SIGNUP inside a rolled-back transaction.

    Inserts the `auth.users` row GoTrue's `/auth/v1/signup` inserts — same
    columns, same `raw_user_meta_data` — so the `on_auth_user_created` and
    `auto_confirm_email_on_signup` triggers fire exactly as they do for a real
    registration. Yields `(cursor, user_id)` already impersonating the new
    account, so a test can ask what a stranger sees the second they sign up.
    """

    @contextmanager
    def _signup(metadata: dict[str, Any] | None) -> Iterator[tuple[Any, str]]:
        db_connection.rollback()
        cursor = db_connection.cursor()
        user_id = str(uuid.uuid4())
        try:
            cursor.execute(
                """
                insert into auth.users (
                    id, instance_id, aud, role, email, encrypted_password,
                    raw_app_meta_data, raw_user_meta_data, created_at, updated_at
                ) values (
                    %s, '00000000-0000-0000-0000-000000000000', 'authenticated',
                    'authenticated', %s, 'not-a-real-hash',
                    '{"provider":"email"}'::jsonb, %s::jsonb, now(), now()
                )
                """,
                (
                    user_id,
                    f"security-probe+{user_id[:8]}@example.invalid",
                    json.dumps(metadata or {}),
                ),
            )
            claims = {"sub": user_id, "role": "authenticated", "aud": "authenticated"}
            cursor.execute(
                "select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),)
            )
            cursor.execute("set local role authenticated")
            yield cursor, user_id
        finally:
            db_connection.rollback()

    return _signup


def scalar(cursor: Any, sql: str, params: tuple[Any, ...] | None = None) -> Any:
    """First column of the first row. The shape every probe here wants."""
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return None if row is None else row[0]
