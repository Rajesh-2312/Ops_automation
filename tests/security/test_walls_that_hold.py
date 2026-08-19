"""Controls that survived the review. Nothing here is xfail; a red test is a breach.

A security suite that only records failures is half a suite. These are the walls
the review attacked and could not move, pinned so that a later refactor cannot
quietly undo one:

  * R5, the commercials wall, for the LDE Executive — at the DATABASE, not the UI.
  * §4's trainer sentinel: migration 1800 dropped all eighteen trainer policies,
    including the four writes, two of which let the payee mark the attendance
    that decided their own pay.
  * The `anon` role: zero rows everywhere, with the anon key being public.
  * The college views, whose WHERE clause is their entire access control (0800).
"""

from __future__ import annotations

import pytest

from tests.security.conftest import COMMERCIAL_TABLES, USERS, scalar

pytestmark = [pytest.mark.rls, pytest.mark.integration]

#: Every table an internal persona can name, plus the three curated college views.
ALL_READABLE = (
    "pnl",
    "remuneration_sheets",
    "work_orders",
    "trainer_bank_accounts",
    "trainers",
    "artifact_versions",
    "program_documents",
    "comms_messages",
    "clusters",
    "colleges",
    "programs",
    "deployments",
    "trainer_attendance",
    "user_college_assignments",
    "user_cluster_assignments",
    "audit_events",
    "tasks",
    "erm_sync_tasks",
    "rag_documents",
    "rag_chunks",
    "students",
    "batches",
    "college_program_progress",
    "college_attendance_summary",
    "college_governance_reports",
    "task_urgency",
)


# --- R5: the commercials wall -------------------------------------------------


@pytest.mark.parametrize("who", ["lde_demo_inst", "lde_malineni"])
@pytest.mark.parametrize("table", COMMERCIAL_TABLES)
def test_lde_executive_gets_zero_rows_from_every_commercial_table(
    as_user, who: str, table: str
) -> None:
    """CLAUDE.md §4: "An LDE Executive gets **zero rows** from `pnl`, remuneration,
    invoices and work-order rates, in the database rather than in the UI."

    Asserted from an impersonated `authenticated` session so the policy is what is
    being tested, not a Python filter above it.
    """
    with as_user(USERS[who]) as cur:
        assert scalar(cur, "select public.can_see_commercials()") is False
        assert scalar(cur, f"select count(*) from public.{table}") == 0


def test_lde_executive_cannot_write_a_commercial_row(as_user) -> None:
    """The wall is not read-only theatre: writes are refused too."""
    with as_user(USERS["lde_demo_inst"]) as cur:
        cur.execute("update public.trainer_bank_accounts set ifsc = 'ATTK0000001'")
        assert cur.rowcount == 0
        cur.execute("update public.work_orders set rate = 1")
        assert cur.rowcount == 0


# --- §4: trainers are records, not users --------------------------------------


def test_trainer_sentinel_reads_nothing_but_its_own_profile(as_new_signup) -> None:
    """Migration 1800 left `trainer` carrying no policy anywhere.

    Signing up with no role metadata lands on the sentinel. It must see its own
    `profiles` row (so the app can render "you have no access yet") and nothing
    else in the entire schema.
    """
    with as_new_signup({}) as (cur, user_id):
        assert (
            scalar(cur, "select role from public.profiles where id = %s", (user_id,)) == "trainer"
        )
        assert scalar(cur, "select count(*) from public.profiles") == 1

        leaked = {
            table: scalar(cur, f"select count(*) from public.{table}") for table in ALL_READABLE
        }
        assert all(count == 0 for count in leaked.values()), {
            table: count for table, count in leaked.items() if count
        }


def test_trainer_sentinel_cannot_mark_attendance_or_touch_money(as_new_signup) -> None:
    """The two writes 1800 was written to remove.

    Before 1800 a trainer could mark the attendance that decided their own pay.
    Any non-zero rowcount here is that hole reopening.
    """
    with as_new_signup({}) as (cur, _):
        cur.execute("update public.trainer_attendance set mark = 'P'")
        assert cur.rowcount == 0, "the payee marked the attendance that decides their pay"

        cur.execute("update public.trainers set pan = 'ZZZZZ0000Z'")
        assert cur.rowcount == 0

        cur.execute("""
            insert into public.trainer_bank_accounts
                (trainer_id, bank_account_number, ifsc, bank_name, account_name)
            select id, '1', 'ABCD0000001', 'b', 'a' from public.trainers limit 1
            """)
        assert cur.rowcount == 0


def test_no_user_may_promote_their_own_profile(as_new_signup) -> None:
    """`profiles_self_update` is open by row, so the guard trigger is the control.

    `profiles_guard_privileged_columns` must refuse role / is_admin / trainer_id /
    college_id for anyone who is not already an admin. Without it, SEC-01 would not
    even need the signup form.
    """
    import psycopg

    for column, value in (
        ("role", "'senior_manager'"),
        ("is_admin", "true"),
        ("college_id", "'1641c9c0-1d30-4b0f-b7c0-3c9a86419a6d'::uuid"),
    ):
        with (
            as_new_signup({}) as (cur, user_id),
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cur.execute(f"update public.profiles set {column} = {value} where id = %s", (user_id,))


# --- the anon role ------------------------------------------------------------


@pytest.mark.parametrize("table", ["trainers", "profiles", "programs", "remuneration_sheets"])
def test_anon_reads_nothing(as_user, table: str) -> None:
    """The anon key ships to every browser (`frontend/src/lib/supabase.ts`).

    Every policy in the schema is `to authenticated`, so the unauthenticated role
    gets zero rows. This is what makes the anon key safe to publish, and it is the
    single assumption the whole frontend architecture rests on.
    """
    with as_user(None, role="anon") as cur:
        assert scalar(cur, f"select count(*) from public.{table}") == 0


# --- 0800: the college views --------------------------------------------------


def test_college_views_are_scoped_to_the_callers_reach(as_user) -> None:
    """0800 declares these `security_invoker = false`, so their WHERE clause IS the wall.

    An LDE Executive at Demo Institute and one at Malineni must see disjoint
    programs. If a future column or join drops a branch of that filter, this goes
    red before anyone reads the migration header.
    """
    seen: dict[str, set] = {}
    for who in ("lde_demo_inst", "lde_malineni"):
        with as_user(USERS[who]) as cur:
            cur.execute("select program_id from public.college_program_progress")
            seen[who] = {row[0] for row in cur.fetchall()}

    assert seen["lde_demo_inst"], "fixture drift: no programs visible to lde_demo_inst"
    assert seen["lde_malineni"], "fixture drift: no programs visible to lde_malineni"
    assert not (
        seen["lde_demo_inst"] & seen["lde_malineni"]
    ), f"college_program_progress leaked across colleges: {seen}"


def test_unshared_governance_reports_are_invisible_in_the_college_view(as_user) -> None:
    """The publish gate applies to every persona, including internal staff (0800)."""
    with as_user(USERS["sm_admin_malineni"]) as cur:
        unshared = scalar(
            cur,
            """
            select count(*) from public.college_governance_reports v
             where not exists (
               select 1 from public.governance_reports g
                where g.id = v.id and g.shared_with_college_at is not null)
            """,
        )
        assert unshared == 0
