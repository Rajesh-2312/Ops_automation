"""SEC-04 — TRUNCATE is granted to `anon` and `authenticated`, and RLS never covers it.

PostgreSQL applies row-level security to SELECT / INSERT / UPDATE / DELETE. It
does **not** apply to TRUNCATE, which is gated by table privilege alone. So a
`TRUNCATE` grant is an unconditional table-wipe right that no policy in
`supabase/migrations/` can restrain.

The migrations are careful — every one of them writes the minimal form::

    grant select, insert, update, delete on public.trainer_attendance to authenticated;

but Supabase's project-level `ALTER DEFAULT PRIVILEGES` has already granted
`arwdDxtm` (which includes `D` = TRUNCATE) on every new table in `public` to
`anon`, `authenticated` and `service_role`. `pg_default_acl` shows it. Nothing
revokes it, so the explicit narrow grant is additive to a wide one and the
tables end up with TRUNCATE, TRIGGER, REFERENCES and MAINTAIN as well.

STATUS — FIX WRITTEN, NOT YET APPLIED
-------------------------------------
`supabase/migrations/2400_truncate_grant_and_storage_reach.sql` closes this. It
has been verified end to end against this database inside a transaction that
was **rolled back**; it has deliberately NOT been applied, because applying
migrations is the orchestrator's move with the owner present.

So the finding is still live on the database these probes run against, and the
2400 has since been applied, so those markers are gone and these are ordinary
regression tests. The mechanism worked exactly as designed and is worth
recording: each marker was `strict=True`, so applying the fix turned all of
them into hard failures whose message was the instruction to delete them. A
fix could not land while the regression test quietly kept documenting a hole
that was no longer there.

REACHABILITY, STATED HONESTLY
-----------------------------
No remote path to this was demonstrated. PostgREST exposes CRUD and `rpc/`; it
has no way to express TRUNCATE, and `pg_graphql` likewise. No SECURITY INVOKER
function in this schema executes dynamic SQL (the `test` schema from
`supabase/tests/02_rls_matrix_test.sql`, which does, is **not** deployed —
verified against `pg_namespace`). So today this is a latent over-grant rather
than a live exploit: it becomes live the moment anything can run one statement
as `authenticated`.

The tests below therefore assert the PRIVILEGE, not just the behaviour. The
privilege is the defect; the behaviour is only its proof.
"""

from __future__ import annotations

import pytest

from tests.security.conftest import USERS, scalar

psycopg = pytest.importorskip("psycopg", reason="psycopg is needed for RLS probes")

pytestmark = [pytest.mark.rls, pytest.mark.integration]

#: Wiping any of these destroys either the payout inputs, the payout outputs, or
#: the reach map that every policy in the schema resolves against.
CRITICAL_TABLES = (
    "trainer_attendance",
    "remuneration_sheets",
    "work_orders",
    "user_college_assignments",
    "user_cluster_assignments",
    "tasks",
    "deployments",
)

#: The four letters of `arwdDxtm` that no browser session has any use for.
#: TRUNCATE wipes a table with RLS silent; TRIGGER attaches code to a table the
#: grantee does not own; REFERENCES pins rows via a foreign key; MAINTAIN (PG17)
#: is VACUUM / ANALYZE / REINDEX / CLUSTER / REFRESH, each taking a heavy lock.
OVER_GRANTED_PRIVILEGES = ("TRUNCATE", "TRIGGER", "REFERENCES", "MAINTAIN")

#: The four RLS *does* cover. These must survive 2400 untouched — the policies
#: are the control for them, and revoking them would break every persona.
RLS_COVERED_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


# SEC-04 CLOSED: migration 2400 applied 2026-08-19. The xfail(strict=True)
# that stood here named 2400 as NOT YET APPLIED and flipped to a hard failure
# the moment it landed — which is what the marker was for. Deleted rather
# than skipped: from here this is an ordinary regression test.
@pytest.mark.parametrize("grantee", ["authenticated", "anon"])
@pytest.mark.parametrize("table", CRITICAL_TABLES)
def test_no_truncate_privilege_on_critical_tables(db_connection, grantee: str, table: str) -> None:
    """RLS cannot restrain TRUNCATE, so the privilege itself must not exist."""
    db_connection.rollback()
    cur = db_connection.cursor()
    held = scalar(
        cur,
        "select has_table_privilege(%s, %s, 'TRUNCATE')",
        (grantee, f"public.{table}"),
    )
    assert held is False, (
        f"{grantee} holds TRUNCATE on public.{table}. No RLS policy applies to "
        "TRUNCATE, so this is an unconditional wipe right."
    )


# SEC-04 CLOSED: migration 2400 applied 2026-08-19. The xfail(strict=True)
# that stood here named 2400 as NOT YET APPLIED and flipped to a hard failure
# the moment it landed — which is what the marker was for. Deleted rather
# than skipped: from here this is an ordinary regression test.
@pytest.mark.parametrize("privilege", OVER_GRANTED_PRIVILEGES)
def test_no_relation_in_public_carries_the_over_grant(db_connection, privilege: str) -> None:
    """The drift guard: sweep EVERY relation in `public`, not a curated list.

    `CRITICAL_TABLES` above names the tables whose loss hurts most, which makes
    it a good headline and a bad invariant — it cannot notice a table added next
    month. This one asks the catalogue instead.

    It also covers the case 2400's header calls out as beyond its reach: the
    project carries a second `pg_default_acl` entry whose grantor is
    `supabase_admin`, and `postgres` is not a member of that role and cannot
    amend it. A table created in `public` BY `supabase_admin` would therefore
    still arrive with the wide grant. Nothing in this project creates tables
    that way — and if something ever does, it surfaces here rather than nowhere.
    """
    db_connection.rollback()
    cur = db_connection.cursor()
    cur.execute(
        """
        select n.nspname || '.' || c.relname, g.grantee
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
          cross join (values ('anon'), ('authenticated')) as g(grantee)
         where n.nspname = 'public'
           and c.relkind in ('r', 'p', 'v', 'm', 'f')
           and has_table_privilege(g.grantee, c.oid, %s)
         order by 1, 2
        """,
        (privilege,),
    )
    offenders = cur.fetchall()
    assert offenders == [], (
        f"{len(offenders)} relation/grantee pairs still carry {privilege} in public, "
        f"e.g. {offenders[:3]}"
    )


# SEC-04 CLOSED: migration 2400 applied 2026-08-19. The xfail(strict=True)
# that stood here named 2400 as NOT YET APPLIED and flipped to a hard failure
# the moment it landed — which is what the marker was for. Deleted rather
# than skipped: from here this is an ordinary regression test.
def test_a_newly_created_table_does_not_inherit_the_over_grant(db_connection) -> None:
    """Revoking on today's tables fixes today. The default privilege fixes tomorrow.

    Without the `alter default privileges` half, the next `create table` in
    `public` silently re-acquires TRUNCATE and this whole finding reads as
    closed while being reopened. So the probe does not read `pg_default_acl` and
    reason about it — it creates a table and asks.

    Created and dropped inside the transaction this suite never commits.
    """
    db_connection.rollback()
    cur = db_connection.cursor()
    try:
        cur.execute("create table public._sec04_default_privilege_probe (id int)")
        for grantee in ("anon", "authenticated"):
            for privilege in OVER_GRANTED_PRIVILEGES:
                held = scalar(
                    cur,
                    "select has_table_privilege(%s, 'public._sec04_default_privilege_probe', %s)",
                    (grantee, privilege),
                )
                assert held is False, (
                    f"a table created after the fix still hands {grantee} {privilege}. "
                    "`alter default privileges in schema public revoke ...` is missing "
                    "or was written FOR a role other than the one that creates tables."
                )
        # SELECT is expected to survive — this is a narrowing, not a lockout.
        assert (
            scalar(
                cur,
                "select has_table_privilege('authenticated', "
                "'public._sec04_default_privilege_probe', 'SELECT')",
            )
            is True
        ), "the default privilege revoke went too wide and took SELECT with it"
    finally:
        db_connection.rollback()


# SEC-04 CLOSED: migration 2400 applied 2026-08-19. The xfail(strict=True)
# that stood here named 2400 as NOT YET APPLIED and flipped to a hard failure
# the moment it landed — which is what the marker was for. Deleted rather
# than skipped: from here this is an ordinary regression test.
def test_lde_executive_cannot_wipe_the_attendance_table(db_connection, as_user) -> None:
    """An LDE Executive holds no policy over other campuses; TRUNCATE ignores that.

    `trainer_attendance` is the payout input (§5, §6): every payable day for every
    trainer in the estate. Losing it means no payout cycle can be computed or
    defended.

    Two assertions, and both matter. The row count is re-read as the session role
    (BYPASSRLS) so it is about the table rather than about what the impersonated
    persona can see — but a count alone is not enough here, because once the
    privilege is revoked the TRUNCATE *raises* instead of succeeding, and a raise
    is indistinguishable from an assertion failure to `xfail`. Without asserting
    the refusal explicitly this test would stay xfail forever and never signal
    that the fix had landed. So the refusal is caught on a savepoint and asserted
    by name.

    Everything runs inside the transaction the fixture rolls back.
    """
    with as_user(USERS["lde_demo_inst"]) as cur:
        before = scalar(cur, "select count(*) from public.trainer_attendance")
        assert before and before > 0, "fixture drift: no attendance rows to protect"

        cur.execute("savepoint sec04_truncate_probe")
        refused: str | None = None
        try:
            cur.execute("truncate table public.trainer_attendance cascade")
        except psycopg.errors.InsufficientPrivilege as exc:
            refused = exc.sqlstate
            cur.execute("rollback to savepoint sec04_truncate_probe")

        cur.execute("reset role")  # observe as postgres; still inside the rolled-back tx
        after = scalar(cur, "select count(*) from public.trainer_attendance")

        assert refused == "42501", (
            "an LDE Executive was allowed to issue TRUNCATE on trainer_attendance. "
            "RLS does not apply to TRUNCATE, so the table privilege is the only control."
        )
        assert (
            after == before
        ), f"an LDE Executive truncated trainer_attendance: {before} rows -> {after}."


@pytest.mark.parametrize("table", CRITICAL_TABLES)
def test_the_verbs_rls_does_cover_are_left_alone(db_connection, table: str) -> None:
    """The other half of the fix: 2400 must narrow, not lock out.

    SELECT / INSERT / UPDATE / DELETE are exactly the four verbs RLS *does*
    cover, which makes the policies the control for them and makes revoking them
    here both unnecessary and catastrophic — an LDE Executive who cannot mark
    attendance is a platform nobody uses. This passes before 2400 and must still
    pass after it; it is the test that catches a `revoke all` written where a
    `revoke truncate, trigger, references, maintain` was meant.
    """
    db_connection.rollback()
    cur = db_connection.cursor()
    for privilege in RLS_COVERED_PRIVILEGES:
        held = scalar(
            cur,
            "select has_table_privilege('authenticated', %s, %s)",
            (f"public.{table}", privilege),
        )
        assert held is True, (
            f"authenticated lost {privilege} on public.{table}. RLS covers this verb; "
            "the policy is the control, and removing the grant breaks every persona."
        )


def test_the_bypassrls_backend_path_is_not_narrowed(db_connection) -> None:
    """`service_role` keeps what it had, deliberately.

    `app/db/session.py` connects with a BYPASSRLS credential — that is the
    intended FastAPI path, where the Python guard is the wall (CLAUDE.md §11,
    technique 2). Narrowing it would break the backend without closing anything,
    because a BYPASSRLS role is already past every policy. Asserted so that a
    later "revoke from everyone" cannot be mistaken for thoroughness.
    """
    db_connection.rollback()
    cur = db_connection.cursor()
    assert (
        scalar(
            cur,
            "select has_table_privilege('service_role', 'public.trainer_attendance', 'TRUNCATE')",
        )
        is True
    )
    assert scalar(cur, "select rolbypassrls from pg_roles where rolname = 'service_role'") is True


def test_audit_events_is_protected_from_truncate(db_connection) -> None:
    """The control that WORKS, locked in.

    `audit_events` is the one table that got this right, twice over: the grant is
    SELECT only, and `audit_events_no_truncate` is a BEFORE TRUNCATE statement
    trigger that raises regardless. §11's audit trail survives what the rest of
    the schema does not — this test keeps it that way.
    """
    db_connection.rollback()
    cur = db_connection.cursor()
    for grantee in ("authenticated", "anon"):
        held = scalar(
            cur,
            "select has_table_privilege(%s, 'public.audit_events', 'TRUNCATE')",
            (grantee,),
        )
        assert held is False, f"{grantee} gained TRUNCATE on audit_events"

    trigger = scalar(
        cur,
        """
        select count(*) from pg_trigger t
         join pg_class c on c.oid = t.tgrelid
         where c.relname = 'audit_events' and t.tgname = 'audit_events_no_truncate'
           and not t.tgisinternal
        """,
    )
    assert trigger == 1, "audit_events_no_truncate trigger is missing"


def test_rag_embeddings_is_fail_closed(db_connection) -> None:
    """Another control that WORKS: `rag_embeddings` has RLS on and zero policies.

    Zero policies under FORCE ROW LEVEL SECURITY denies everything, and no SELECT
    grant exists for `authenticated` either. Vector rows are reachable only
    through `public.rag_search()`, which applies §9's persona filter in the same
    statement as the `ORDER BY ... LIMIT`. Asserted so a future "fix the empty
    policy list" change has to be deliberate.
    """
    db_connection.rollback()
    cur = db_connection.cursor()
    policies = scalar(
        cur,
        "select count(*) from pg_policies where schemaname='public' and tablename='rag_embeddings'",
    )
    assert policies == 0
    assert (
        scalar(
            cur, "select has_table_privilege('authenticated', 'public.rag_embeddings', 'SELECT')"
        )
        is False
    )
