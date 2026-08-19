"""The last-admin guard — `supabase/migrations/2000_last_admin_guard.sql`.

Two suites in one file, because they defend the same thing from different sides.

STATIC (always runs, no database)
    Reads the migration text. Its job is to fail loudly if someone later
    "tidies" the guard into consistency with 0200 by adding an
    `auth.uid() is null` short-circuit — which would make it silent on exactly
    the connection that caused the 2026-08-15 incident.

LIVE (opt-in)
    The real proof. Applies 2000 to the real database inside a transaction that
    is ALWAYS rolled back, then drives the guard through every path in the
    migration header: self-demotion, combined role change, profile delete,
    account delete, one-statement mass demotion, and the two cases that prove
    the guard is not simply "no demotions ever" — a non-last admin can be
    demoted, and 1200's bootstrap still recovers a zero-admin database.

    Opt in with:

        BYTEXL_DB_TESTS=1 python -m pytest -q tests/rls/test_last_admin_guard.py

    It is NOT part of the default `pytest -q` run on purpose. It writes to the
    live `profiles` table (inside the rolled-back transaction) and briefly holds
    row locks on it; every RLS helper in the schema reads that table, so a
    default-on database test would put a lock on the platform's hot path every
    time anyone ran the unit suite. Same reasoning as `run_tests.py` being an
    explicit command rather than a pytest hook.

    Nothing here disables a trigger, so no ACCESS EXCLUSIVE lock is taken. The
    zero-admin state the recovery test needs is reached with
    `set constraints all deferred` instead, which is the reason 2000 uses a
    deferrable constraint trigger in the first place.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import errors as pg_errors

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "supabase" / "migrations"
GUARD_SQL = MIGRATIONS / "2000_last_admin_guard.sql"
BOOTSTRAP_SQL = MIGRATIONS / "1200_backfill_profiles_and_bootstrap_admin.sql"
README = MIGRATIONS / "README.md"

OPT_IN = "BYTEXL_DB_TESTS"

# Fixture accounts. Distinct prefix from 01_fixtures.sql's `f0…` so a leaked row
# from either suite is attributable at a glance.
ADMIN_A = "2a000000-0000-0000-0000-00000000000a"  # senior_manager, the lone admin
SPARE_B = "2a000000-0000-0000-0000-00000000000b"  # senior_manager, promotable
MANAGER_C = "2a000000-0000-0000-0000-00000000000c"  # manager: cannot hold is_admin

SEED = f"""
insert into auth.users (id, email, raw_user_meta_data) values
  ('{ADMIN_A}',   'lastadmin.a@bytexl.test',
   '{{"role":"senior_manager","full_name":"Guard Fixture A"}}'),
  ('{SPARE_B}',   'lastadmin.b@bytexl.test',
   '{{"role":"senior_manager","full_name":"Guard Fixture B"}}'),
  ('{MANAGER_C}', 'lastadmin.c@bytexl.test',
   '{{"role":"manager","full_name":"Guard Fixture C"}}');

-- Promote the fixture admin BEFORE demoting the live one. Two admins exist in
-- between, so the guard permits the second statement — the setup is itself a
-- demonstration that a non-last admin is demotable, and it means this suite
-- never has to disable a trigger to reach its baseline.
update public.profiles set is_admin = true  where id = '{ADMIN_A}';
update public.profiles set is_admin = false where is_admin and id <> '{ADMIN_A}';
"""


# --------------------------------------------------------------------------- #
# Static — no database
# --------------------------------------------------------------------------- #


def test_migration_ships_and_is_registered() -> None:
    """The file exists under its reserved number and the README lists it."""
    assert GUARD_SQL.exists(), f"missing {GUARD_SQL}"
    assert "2000_last_admin_guard.sql" in README.read_text(encoding="utf-8")


def test_guard_has_no_auth_uid_short_circuit() -> None:
    """The divergence from 0200 is the whole point — see the migration header.

    Every other column guard in this schema returns early on
    `auth.uid() is null`, which means it does nothing on a service-role or SQL
    editor connection. Those are precisely the connections that removed the last
    admin on 2026-08-15. If this assertion ever fails, the guard has been made
    consistent with its neighbours and useless at the same time.
    """
    text = GUARD_SQL.read_text(encoding="utf-8")
    after_header = text.split("create or replace function public.profiles_guard_last_admin", 1)[1]
    # The dollar-quoted body only: the header and the `comment on function` both
    # discuss auth.uid() at length, and neither is code.
    body = after_header.split("$$", 2)[1]
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("--"))
    assert "auth.uid()" not in code


def test_guard_covers_both_update_and_delete() -> None:
    """DELETE is reachable (`profiles_admin_all` is `for all`) and cascades."""
    body = GUARD_SQL.read_text(encoding="utf-8").lower()
    assert "create constraint trigger profiles_guard_last_admin_upd" in body
    assert "create constraint trigger profiles_guard_last_admin_del" in body
    assert "after update on public.profiles" in body
    assert "after delete on public.profiles" in body
    # AFTER, not BEFORE: a BEFORE row trigger cannot see the other rows of its
    # own statement, so `update … set is_admin = false where is_admin` would
    # slip through. There is a live test for that statement below.
    assert "before update on public.profiles" not in body


# --------------------------------------------------------------------------- #
# Live — opt-in, always rolled back
# --------------------------------------------------------------------------- #


def _database_url() -> str:
    env = REPO_ROOT / ".env"
    if not env.exists():
        pytest.skip("no .env at the repo root")
    for line in env.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^DATABASE_URL=(.+)$", line.strip())
        if match:
            url = match.group(1).strip().strip('"').strip("'")
            if not url or "YOUR-PASSWORD" in url:
                pytest.skip("DATABASE_URL in .env is still the placeholder")
            return url
    pytest.skip("DATABASE_URL is not set in .env")


def _admins(conn: psycopg.Connection) -> int:
    """Admins as `public.is_admin()` defines them: the flag AND the persona."""
    row = conn.execute(
        "select count(*) from public.profiles where is_admin and role = 'senior_manager'"
    ).fetchone()
    assert row is not None
    return int(row[0])


def _bootstrap_statement() -> str:
    """1200 Part 2, read from the shipped file rather than copied into this test.

    Copying it would let the migration drift while the test kept passing against
    a fossil. The split marker is 1200's own section header.
    """
    text = BOOTSTRAP_SQL.read_text(encoding="utf-8")
    marker = "--- Part 2: first admin"
    assert marker in text, "1200 changed shape; update this marker"
    return text.split(marker, 1)[1]


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    """One connection, one transaction, rolled back unconditionally.

    The rollback is the entire safety story, exactly as in run_tests.py: it runs
    on pass, on failure and on interrupt, so neither the migration nor the
    fixture accounts nor any demotion below ever reaches a commit.
    """
    if not os.environ.get(OPT_IN):
        pytest.skip(f"live database test; set {OPT_IN}=1 to run it")

    url = _database_url()
    try:
        connection = psycopg.connect(url, connect_timeout=20, autocommit=False)
    except psycopg.OperationalError as exc:  # offline / IPv6-only host
        pytest.skip(f"cannot reach the database: {exc}")

    try:
        with connection.cursor() as cur:
            # No parameters, so psycopg uses the simple protocol and the whole
            # multi-statement file goes over in one round trip.
            cur.execute(GUARD_SQL.read_text(encoding="utf-8"))
            cur.execute(SEED)
        assert _admins(connection) == 1, "baseline must be exactly one admin"
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def _immediate_constraints(request: pytest.FixtureRequest) -> None:
    """Undo any `set constraints all deferred` a previous test left behind.

    SET CONSTRAINTS is transaction-scoped, and two of the tests below deliberately
    defer. Resetting before each test keeps them independent of execution order.
    """
    if "conn" in request.fixturenames and os.environ.get(OPT_IN):
        request.getfixturevalue("conn").execute("set constraints all immediate")


# --- The guard refuses ------------------------------------------------------ #


def test_last_admin_cannot_demote_themselves(conn: psycopg.Connection) -> None:
    """The 2026-08-15 incident, replayed. One boolean, one row, no replacement."""
    with conn.transaction(force_rollback=True):
        with pytest.raises(pg_errors.InsufficientPrivilege) as caught, conn.transaction():
            conn.execute("update public.profiles set is_admin = false where id = %s", (ADMIN_A,))
        message = str(caught.value)
        assert "no admin" in message
        assert "Promote a replacement FIRST" in message, "the fix must be in the message"
        assert _admins(conn) == 1


def test_last_admin_cannot_be_demoted_by_a_combined_role_change(
    conn: psycopg.Connection,
) -> None:
    """`set role = 'manager', is_admin = false` is path 1 wearing a hat.

    Counting admins rather than watching columns is what catches this without
    enumerating it.
    """
    with conn.transaction(force_rollback=True):
        with pytest.raises(pg_errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "update public.profiles set role = 'manager', is_admin = false " "where id = %s",
                (ADMIN_A,),
            )
        assert _admins(conn) == 1


def test_role_change_alone_cannot_strip_the_last_admin(conn: psycopg.Connection) -> None:
    """Path 4 of the header: already closed, by `profiles_admin_ck` in 0200.

    Asserted here so the claim is checked rather than believed. If that CHECK is
    ever relaxed this fails, and the guard above takes over — it tests
    `is_admin AND role = 'senior_manager'` for exactly that reason.
    """
    with conn.transaction(force_rollback=True):
        with pytest.raises(pg_errors.CheckViolation) as caught, conn.transaction():
            conn.execute("update public.profiles set role = 'manager' where id = %s", (ADMIN_A,))
        assert "profiles_admin_ck" in str(caught.value)
        assert _admins(conn) == 1


def test_last_admin_profile_cannot_be_deleted(conn: psycopg.Connection) -> None:
    """`profiles_admin_all` is `for all`, so DELETE is a real path to zero."""
    with conn.transaction(force_rollback=True):
        with pytest.raises(pg_errors.InsufficientPrivilege), conn.transaction():
            conn.execute("delete from public.profiles where id = %s", (ADMIN_A,))
        assert _admins(conn) == 1


def test_last_admin_account_cannot_be_deleted(conn: psycopg.Connection) -> None:
    """Deleting the ACCOUNT is the same lockout: profiles cascades from auth.users.

    The cascade fires the row trigger on `profiles`, which is why one trigger
    covers both and no trigger on `auth.users` is needed.
    """
    with conn.transaction(force_rollback=True):
        with pytest.raises(pg_errors.InsufficientPrivilege), conn.transaction():
            conn.execute("delete from auth.users where id = %s", (ADMIN_A,))
        assert _admins(conn) == 1


def test_mass_demotion_in_one_statement_is_blocked(conn: psycopg.Connection) -> None:
    """The reason this guard is AFTER and not BEFORE.

    A BEFORE row trigger cannot see the other rows of its own statement: each
    firing would see the OTHER admin still flagged and allow the demotion, and
    the table would end adminless with no error.
    """
    with conn.transaction(force_rollback=True):
        conn.execute("update public.profiles set is_admin = true where id = %s", (SPARE_B,))
        assert _admins(conn) == 2

        with pytest.raises(pg_errors.InsufficientPrivilege), conn.transaction():
            conn.execute("update public.profiles set is_admin = false where is_admin")

        assert _admins(conn) == 2


# --- The guard permits ------------------------------------------------------ #


def test_a_non_last_admin_can_be_demoted(conn: psycopg.Connection) -> None:
    """The guard is not "no demotions ever" — it is "not the last one"."""
    with conn.transaction(force_rollback=True):
        conn.execute("update public.profiles set is_admin = true where id = %s", (SPARE_B,))
        assert _admins(conn) == 2

        conn.execute("update public.profiles set is_admin = false where id = %s", (ADMIN_A,))

        assert _admins(conn) == 1
        row = conn.execute(
            "select is_admin from public.profiles where id = %s", (ADMIN_A,)
        ).fetchone()
        assert row is not None and row[0] is False


def test_a_non_last_admin_profile_can_be_deleted(conn: psycopg.Connection) -> None:
    """Same rule on the DELETE side: a replacement makes the removal legal."""
    with conn.transaction(force_rollback=True):
        conn.execute("update public.profiles set is_admin = true where id = %s", (SPARE_B,))

        conn.execute("delete from public.profiles where id = %s", (ADMIN_A,))

        assert _admins(conn) == 1


def test_promotion_is_never_blocked(conn: psycopg.Connection) -> None:
    """Including the role change a Manager needs before they may hold the flag."""
    with conn.transaction(force_rollback=True):
        conn.execute(
            "update public.profiles set role = 'senior_manager', is_admin = true where id = %s",
            (MANAGER_C,),
        )
        assert _admins(conn) == 2


def test_descriptive_edits_to_the_last_admin_still_work(conn: psycopg.Connection) -> None:
    """Over-blocking would be its own outage. The WHEN clause never fires here."""
    with conn.transaction(force_rollback=True):
        conn.execute(
            "update public.profiles set full_name = 'Renamed Admin' where id = %s", (ADMIN_A,)
        )
        assert _admins(conn) == 1


def test_wholesale_teardown_is_allowed(conn: psycopg.Connection) -> None:
    """The empty-table carve-out, which `00_isolate.sql` depends on.

    The R5 persona harness opens with `delete from public.profiles;`. Without
    this branch, installing 2000 would break the suite that proves §4. The cost
    is stated in the migration header: an unqualified delete still empties the
    table.
    """
    with conn.transaction(force_rollback=True):
        conn.execute("delete from public.profiles")

        row = conn.execute("select count(*) from public.profiles").fetchone()
        assert row is not None and row[0] == 0


# --- Recovery and deferral -------------------------------------------------- #


def test_bootstrap_recovery_from_zero_admins_is_not_blocked(conn: psycopg.Connection) -> None:
    """A guard that blocks recovery is worse than the hole it closes.

    Reaching a zero-admin state with the guard installed is only possible by
    deferring it — which is exactly how a legitimate handover would be scripted.
    1200's SHIPPED Part 2 statement then runs against that state, and the check
    passes when constraints go back to immediate.
    """
    with conn.transaction(force_rollback=True):
        conn.execute("set constraints all deferred")
        conn.execute("update public.profiles set is_admin = false where id = %s", (ADMIN_A,))
        assert _admins(conn) == 0, "the demotion must have landed, check merely deferred"

        conn.execute(_bootstrap_statement())

        assert _admins(conn) == 1
        conn.execute("set constraints all immediate")  # would raise if it were still zero


def test_deferred_demotion_without_a_replacement_still_fails(conn: psycopg.Connection) -> None:
    """Deferring moves WHEN the invariant is checked, never WHETHER it holds."""
    with conn.transaction(force_rollback=True):
        with pytest.raises(pg_errors.InsufficientPrivilege), conn.transaction():
            conn.execute("set constraints all deferred")
            conn.execute("update public.profiles set is_admin = false where id = %s", (ADMIN_A,))
            conn.execute("set constraints all immediate")
        conn.execute("set constraints all immediate")
        assert _admins(conn) == 1
