"""Seed and remove a self-contained demo dataset.

    python tools/demo_data.py --create     # build it
    python tools/demo_data.py --status     # what exists right now
    python tools/demo_data.py --delete     # remove every trace

WHY THIS IS A SCRIPT AND NOT A MIGRATION OR A SEED
==================================================
`supabase/seed.sql` is the operating model — 37 task and 37 document templates
that every environment needs. This is the opposite: throwaway rows for a
walkthrough, which must never reach a real environment and must come out
cleanly. Putting it in a migration would make demo trainers permanent history
(§11: never edit a shipped migration), so it lives in `tools/` beside the rule
linter, off the migration path entirely.

HOW DELETION IS MADE EXACT
--------------------------
Every row this script creates gets a UUID derived from `uuid5(DEMO_NAMESPACE,
label)`. That buys three properties at once:

* **Idempotent.** Re-running `--create` writes the same ids, so it upserts
  rather than duplicating.
* **Exact.** `--delete` removes rows by primary key. It never runs a
  `LIKE '%demo%'` or a date-range sweep, either of which would eventually take
  a real row with it — which is the failure mode that makes people stop
  trusting cleanup scripts and start leaving demo data in place forever.
* **Auditable.** `--status` recomputes the ids and reports what is present, so
  "did the demo data get removed?" is answerable without a memory of what was
  created.

The namespace is a fixed literal. Do not regenerate it — its whole job is to be
the same on the machine that creates the data and the machine that removes it.

WHAT IT REFUSES TO TOUCH
------------------------
Nothing outside the id set below, and it asserts that: `--delete` reports what
it removed, and any real row that happened to be nearby is simply not addressed.
The live database this was written against holds one real college, one real
program, two real batches, one real trainer and two real user accounts. None of
them share an id with anything here.

AUTH ACCOUNTS ARE REAL ACCOUNTS
-------------------------------
The six persona logins are created through Supabase's admin API, so they are
genuine `auth.users` rows with working passwords — the point of the demo is to
sign in as an LDE Executive and see the commercials wall hold. They use the
`@demo.bytexl.in` domain and a shared password printed once at creation. That is
acceptable for a demo tenant and unacceptable anywhere else, which is the other
reason this is a script somebody has to run deliberately rather than a seed that
runs itself.

Deleting an `auth.users` row cascades to `profiles` and to both assignment
tables, so the persona side needs no manual ordering. The domain side does, and
`--delete` walks it deliberately: attendance, deployments and work orders come
out before trainers, because 0400 holds trainers down with RESTRICT precisely so
a trainer with history cannot vanish underneath a payout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("psycopg is not installed. Run inside the project venv.")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: Fixed. Every demo id derives from it; regenerating it orphans existing rows.
DEMO_NAMESPACE: Final[uuid.UUID] = uuid.UUID("d3705ea1-0000-4000-8000-000000000001")

DEMO_EMAIL_DOMAIN: Final[str] = "demo.bytexl.in"
DEMO_PASSWORD: Final[str] = "DemoPass!2026"

#: The demo runs over July 2026 so it lines up with the CLAUDE.md §6 fixtures
#: and with `docs/legacy-sheet-findings.md`. A payout period inside a month the
#: reader already has reconciled numbers for is worth more in a walkthrough than
#: an arbitrary recent month.
PERIOD_START: Final[dt.date] = dt.date(2026, 7, 1)
PERIOD_END: Final[dt.date] = dt.date(2026, 7, 31)
WO_FROM: Final[dt.date] = dt.date(2026, 4, 1)
WO_TO: Final[dt.date] = dt.date(2027, 3, 31)


def demo_id(label: str) -> uuid.UUID:
    """The id for `label`. Stable across machines and runs — that is the point."""
    return uuid.uuid5(DEMO_NAMESPACE, label)


# --- what gets created --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemoUser:
    label: str
    email: str
    full_name: str
    role: str

    @property
    def id(self) -> uuid.UUID:
        return demo_id(self.label)


#: 2 Senior Managers, 2 Managers, 2 LDE Executives.
#:
#: The personas are the demo. §4's wall is invisible until you can sign in as an
#: LDE Executive and watch the Payouts nav entry not be there, so two of each
#: exist to show that reach differs BETWEEN people of the same persona — SM-2 and
#: MGR-2 are deliberately left with no assignment, which is what makes "persona
#: is not reach" visible rather than merely stated.
DEMO_USERS: Final[tuple[DemoUser, ...]] = (
    DemoUser("user:sm1", f"sm1@{DEMO_EMAIL_DOMAIN}", "Demo Senior Manager One", "senior_manager"),
    DemoUser("user:sm2", f"sm2@{DEMO_EMAIL_DOMAIN}", "Demo Senior Manager Two", "senior_manager"),
    DemoUser("user:mgr1", f"mgr1@{DEMO_EMAIL_DOMAIN}", "Demo Manager One", "manager"),
    DemoUser("user:mgr2", f"mgr2@{DEMO_EMAIL_DOMAIN}", "Demo Manager Two", "manager"),
    DemoUser("user:lde1", f"lde1@{DEMO_EMAIL_DOMAIN}", "Demo LDE Executive One", "lde_executive"),
    DemoUser("user:lde2", f"lde2@{DEMO_EMAIL_DOMAIN}", "Demo LDE Executive Two", "lde_executive"),
)


@dataclass(frozen=True, slots=True)
class DemoTrainer:
    label: str
    pan: str
    name: str
    kind: str
    rate: Decimal
    rate_basis: str
    account: str
    ifsc: str
    bank: str

    @property
    def id(self) -> uuid.UUID:
        return demo_id(self.label)


#: Five educators. Four bCAP (per month) and one CRT (per day) — §5's central
#: branch is the single most important thing to be able to show side by side,
#: because the two rate bases produce different attendance semantics and a demo
#: that only shows one teaches the wrong mental model.
#:
#: PANs are structurally valid (AAAAA9999A) and deliberately fictitious. The
#: first two rates match the §6 regression fixtures, so a viewer can reconcile
#: the generated sheet against the numbers written in CLAUDE.md.
DEMO_TRAINERS: Final[tuple[DemoTrainer, ...]] = (
    DemoTrainer(
        "trainer:1",
        "DEMAA1111A",
        "Demo Educator Anitha Rao",
        "freelancer",
        Decimal("80000"),
        "per_month",
        "100200300400",
        "SBIN0001234",
        "State Bank of India",
    ),
    DemoTrainer(
        "trainer:2",
        "DEMBB2222B",
        "Demo Educator Bhaskar Nair",
        "freelancer",
        Decimal("65000"),
        "per_month",
        "100200300401",
        "HDFC0004321",
        "HDFC Bank",
    ),
    DemoTrainer(
        "trainer:3",
        "DEMCC3333C",
        "Demo Educator Chitra Menon",
        "full_timer",
        Decimal("70000"),
        "per_month",
        "100200300402",
        "ICIC0005678",
        "ICICI Bank",
    ),
    DemoTrainer(
        "trainer:4",
        "DEMDD4444D",
        "Demo Educator Dinesh Kumar",
        "freelancer",
        Decimal("55000"),
        "per_month",
        "100200300403",
        "UTIB0009012",
        "Axis Bank",
    ),
    DemoTrainer(
        "trainer:5",
        "DEMEE5555E",
        "Demo Educator Elango Subramanian",
        "freelancer",
        Decimal("3500"),
        "per_day",
        "100200300404",
        "KKBK0003456",
        "Kotak Mahindra Bank",
    ),
)

DEMO_STUDENTS: Final[tuple[tuple[str, str], ...]] = (
    ("Demo Student Aarthi Krishnan", "DEMO24CS001"),
    ("Demo Student Bharath Reddy", "DEMO24CS002"),
    ("Demo Student Chaitra Hegde", "DEMO24CS003"),
    ("Demo Student Deepak Varma", "DEMO24CS004"),
    ("Demo Student Esha Pillai", "DEMO24CS005"),
)

CLUSTER_ID: Final[uuid.UUID] = demo_id("cluster:south")
COLLEGE_ID: Final[uuid.UUID] = demo_id("college:1")
PROGRAM_ID: Final[uuid.UUID] = demo_id("program:1")
BATCH_ID: Final[uuid.UUID] = demo_id("batch:1")


# --- plumbing -----------------------------------------------------------------


def load_env() -> dict[str, str]:
    env_file = REPO_ROOT / ".env"
    values = dict(os.environ)
    if env_file.exists():
        for key, value in re.findall(r"^([A-Z_]+)=(.*)$", env_file.read_text(), re.M):
            values.setdefault(key, value.strip())
    missing = [
        k
        for k in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
        if not values.get(k)
    ]
    if missing:
        sys.exit(f"missing from .env: {', '.join(missing)}")
    return values


def admin_call(
    env: dict[str, str], path: str, *, body: Any = None, method: str = "GET"
) -> tuple[int, Any]:
    """One Supabase admin API call. Service role key — never leaves this process."""
    request = urllib.request.Request(
        env["SUPABASE_URL"].rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    key = env["SUPABASE_SERVICE_ROLE_KEY"]
    request.add_header("apikey", key)
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def say(message: str) -> None:
    print(f"  {message}")


# --- create -------------------------------------------------------------------


def create(env: dict[str, str]) -> None:
    print("\nCreating demo data\n" + "-" * 60)

    print("\nAuth accounts")
    for user in DEMO_USERS:
        status, payload = admin_call(
            env,
            "/auth/v1/admin/users",
            method="POST",
            body={
                "id": str(user.id),
                "email": user.email,
                "password": DEMO_PASSWORD,
                "email_confirm": True,
                "user_metadata": {"role": user.role, "full_name": user.full_name},
            },
        )
        if status in (200, 201):
            say(f"created  {user.email:28} {user.role}")
        elif status in (409, 422):
            say(f"exists   {user.email:28} {user.role}")
        else:
            say(f"FAILED   {user.email:28} {status} {payload}")

    with psycopg.connect(env["DATABASE_URL"], connect_timeout=30) as conn, conn.cursor() as cur:
        # The signup trigger writes profiles.role from user_metadata, but an
        # account that already existed keeps whatever it had. Assert the persona
        # rather than assume the trigger ran the way this script wanted.
        for user in DEMO_USERS:
            cur.execute(
                "update public.profiles set role = %s, full_name = %s where id = %s",
                (user.role, user.full_name, str(user.id)),
            )

        print("\nOrg")
        cur.execute(
            "insert into public.clusters (id, name) values (%s, %s) "
            "on conflict (id) do update set name = excluded.name",
            (str(CLUSTER_ID), "Demo Cluster — South"),
        )
        cur.execute(
            "insert into public.colleges (id, name, city, cluster_id) values (%s, %s, %s, %s) "
            "on conflict (id) do update set name = excluded.name, cluster_id = excluded.cluster_id",
            (str(COLLEGE_ID), "Demo Institute of Technology", "Hyderabad", str(CLUSTER_ID)),
        )
        cur.execute(
            "insert into public.programs (id, college_id, type, name, start_date, end_date) "
            "values (%s, %s, %s, %s, %s, %s) on conflict (id) do update set name = excluded.name",
            (
                str(PROGRAM_ID),
                str(COLLEGE_ID),
                "bCAP",
                "Demo bCAP 2026-27 CSE",
                PERIOD_START,
                dt.date(2026, 12, 20),
            ),
        )
        cur.execute(
            "insert into public.batches (id, program_id, name, branch, section, passout_year, "
            "expected_student_count) values (%s, %s, %s, %s, %s, %s, %s) "
            "on conflict (id) do update set passout_year = excluded.passout_year",
            (str(BATCH_ID), str(PROGRAM_ID), "Demo CSE-A", "CSE", "A", 2027, 5),
        )
        say("cluster, college, program, batch")

        print("\nReach")
        # SM-1 covers the cluster; MGR-1 and both LDEs cover the college.
        # SM-2 and MGR-2 get NOTHING on purpose — see the DEMO_USERS note.
        cur.execute(
            "insert into public.user_cluster_assignments (user_id, cluster_id) values (%s, %s) "
            "on conflict (user_id, cluster_id) do nothing",
            (str(demo_id("user:sm1")), str(CLUSTER_ID)),
        )
        for label in ("user:mgr1", "user:lde1", "user:lde2"):
            cur.execute(
                "insert into public.user_college_assignments (user_id, college_id) "
                "values (%s, %s) on conflict (user_id, college_id) do nothing",
                (str(demo_id(label)), str(COLLEGE_ID)),
            )
        say("SM-1 -> cluster · MGR-1, LDE-1, LDE-2 -> college")
        say("SM-2 and MGR-2 left unassigned (persona is not reach)")

        print("\nStudents")
        for index, (name, roll) in enumerate(DEMO_STUDENTS, start=1):
            cur.execute(
                "insert into public.students (id, batch_id, full_name, roll_number) "
                "values (%s, %s, %s, %s) "
                "on conflict (id) do update set full_name = excluded.full_name",
                (str(demo_id(f"student:{index}")), str(BATCH_ID), name, roll),
            )
        say(f"{len(DEMO_STUDENTS)} students in Demo CSE-A")

        print("\nEducators")
        for trainer in DEMO_TRAINERS:
            cur.execute(
                # `zoho_id` is not decoration: §7 makes "ZOHO account exists" a
                # BLOCKING gate, so a demo trainer without one cannot be paid
                # and the walkthrough dies at the commit step with a refusal
                # that looks like a bug and is actually the gate working.
                "insert into public.trainers "
                "(id, pan, full_name, type, email, zoho_id, work_order_status) "
                "values (%s, %s, %s, %s, %s, %s, %s) "
                "on conflict (id) do update set "
                "full_name = excluded.full_name, zoho_id = excluded.zoho_id, "
                "work_order_status = excluded.work_order_status",
                (
                    str(trainer.id),
                    trainer.pan,
                    trainer.name,
                    trainer.kind,
                    f"{trainer.label.replace(':', '')}@{DEMO_EMAIL_DOMAIN}",
                    f"ZOHO-DEMO-{trainer.label.rsplit(':', 1)[1]}",
                    "signed",
                ),
            )
            cur.execute(
                "insert into public.trainer_bank_accounts (trainer_id, bank_account_number, ifsc, "
                "bank_name, account_name) values (%s, %s, %s, %s, %s) "
                "on conflict (trainer_id) do update set ifsc = excluded.ifsc",
                (str(trainer.id), trainer.account, trainer.ifsc, trainer.bank, trainer.name),
            )
            cur.execute(
                "insert into public.work_orders (id, trainer_id, program_id, rate, rate_basis, "
                "valid_from, valid_to, status) values (%s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (id) do update set rate = excluded.rate",
                (
                    str(demo_id(f"wo:{trainer.label}")),
                    str(trainer.id),
                    str(PROGRAM_ID),
                    trainer.rate,
                    trainer.rate_basis,
                    WO_FROM,
                    WO_TO,
                    "signed",
                ),
            )
            cur.execute(
                "insert into public.deployments (id, trainer_id, batch_id, start_date, end_date) "
                "values (%s, %s, %s, %s, %s) on conflict (id) do nothing",
                (
                    str(demo_id(f"dep:{trainer.label}")),
                    str(trainer.id),
                    str(BATCH_ID),
                    PERIOD_START,
                    PERIOD_END,
                ),
            )
        say(f"{len(DEMO_TRAINERS)} educators with bank rails, signed work orders, deployments")
        say("4 bCAP (per month) + 1 CRT (per day) — §5's branch, visible side by side")

        print("\nAttendance (July 2026)")
        # Marked P on weekdays, HOLIDAY at weekends, and one trainer left with a
        # gap on purpose: an unmarked day is the §5 trap (silently pays bCAP,
        # silently underpays CRT) and the Delivery Monitor should have something
        # real to flag during a walkthrough.
        marked = 0
        for trainer in DEMO_TRAINERS:
            deployment = str(demo_id(f"dep:{trainer.label}"))
            day = PERIOD_START
            while day <= PERIOD_END:
                leave_gap = trainer.label == "trainer:4" and day.day in (20, 21, 22)
                if not leave_gap:
                    mark = "HOLIDAY" if day.weekday() >= 5 else "P"
                    cur.execute(
                        "insert into public.trainer_attendance "
                        "(id, deployment_id, mark_date, mark) "
                        "values (%s, %s, %s, %s) on conflict (deployment_id, mark_date) "
                        "do update set mark = excluded.mark",
                        (
                            str(demo_id(f"att:{trainer.label}:{day.isoformat()}")),
                            deployment,
                            day,
                            mark,
                        ),
                    )
                    marked += 1
                day += dt.timedelta(days=1)
        say(f"{marked} day marks · Dinesh Kumar left unmarked 20-22 Jul on purpose")

        conn.commit()

    print("\n" + "-" * 60)
    print("Sign in with any of these:")
    for user in DEMO_USERS:
        print(f"  {user.email:28} {user.role:16} password: {DEMO_PASSWORD}")
    print("\nRemove it all with:  python tools/demo_data.py --delete")


# --- delete -------------------------------------------------------------------


def delete(env: dict[str, str]) -> None:
    """Remove every demo row, in dependency order, by primary key."""
    print("\nDeleting demo data\n" + "-" * 60)

    trainer_ids = [str(t.id) for t in DEMO_TRAINERS]
    deployment_ids = [str(demo_id(f"dep:{t.label}")) for t in DEMO_TRAINERS]

    with psycopg.connect(env["DATABASE_URL"], connect_timeout=30) as conn, conn.cursor() as cur:
        # Order matters: 0400 holds trainers down with RESTRICT from deployments
        # and work_orders, deliberately, so that a trainer with history cannot
        # disappear from under a payout. Clear the history first.
        steps: tuple[tuple[str, str, list[str]], ...] = (
            ("trainer_attendance", "deployment_id", deployment_ids),
            ("deployments", "id", deployment_ids),
            ("work_orders", "id", [str(demo_id(f"wo:{t.label}")) for t in DEMO_TRAINERS]),
            ("remuneration_sheets", "trainer_id", trainer_ids),
            ("trainer_bank_accounts", "trainer_id", trainer_ids),
            ("trainers", "id", trainer_ids),
            (
                "students",
                "id",
                [str(demo_id(f"student:{i}")) for i in range(1, len(DEMO_STUDENTS) + 1)],
            ),
            ("batches", "id", [str(BATCH_ID)]),
            ("tasks", "program_id", [str(PROGRAM_ID)]),
            ("program_documents", "program_id", [str(PROGRAM_ID)]),
            ("programs", "id", [str(PROGRAM_ID)]),
            ("colleges", "id", [str(COLLEGE_ID)]),
            ("clusters", "id", [str(CLUSTER_ID)]),
        )
        for table, column, ids in steps:
            try:
                cur.execute(f"delete from public.{table} where {column} = any(%s::uuid[])", (ids,))
                if cur.rowcount:
                    say(f"{table:24} removed {cur.rowcount}")
            except psycopg.errors.UndefinedTable:
                conn.rollback()
        conn.commit()

    # Auth users last. The cascade from auth.users takes profiles and both
    # assignment tables with it, so nothing above had to address them.
    print()
    for user in DEMO_USERS:
        status, _ = admin_call(env, f"/auth/v1/admin/users/{user.id}", method="DELETE")
        say(f"{'removed ' if status in (200, 204) else f'({status}) '}{user.email}")

    print("\n" + "-" * 60)
    print("Demo data removed. Real rows were never addressed — deletion is by id.")


# --- status -------------------------------------------------------------------


def status(env: dict[str, str]) -> None:
    print("\nDemo data present?\n" + "-" * 60)
    checks: tuple[tuple[str, str, list[str]], ...] = (
        ("clusters", "id", [str(CLUSTER_ID)]),
        ("colleges", "id", [str(COLLEGE_ID)]),
        ("programs", "id", [str(PROGRAM_ID)]),
        ("batches", "id", [str(BATCH_ID)]),
        ("students", "batch_id", [str(BATCH_ID)]),
        ("trainers", "id", [str(t.id) for t in DEMO_TRAINERS]),
        ("trainer_bank_accounts", "trainer_id", [str(t.id) for t in DEMO_TRAINERS]),
        ("work_orders", "trainer_id", [str(t.id) for t in DEMO_TRAINERS]),
        ("deployments", "trainer_id", [str(t.id) for t in DEMO_TRAINERS]),
        ("profiles", "id", [str(u.id) for u in DEMO_USERS]),
    )
    with psycopg.connect(env["DATABASE_URL"], connect_timeout=30) as conn, conn.cursor() as cur:
        for table, column, ids in checks:
            cur.execute(
                f"select count(*) from public.{table} where {column} = any(%s::uuid[])", (ids,)
            )
            say(f"{table:24} {cur.fetchone()[0]}")
        cur.execute(
            "select count(*) from public.trainer_attendance where deployment_id = any(%s::uuid[])",
            ([str(demo_id(f"dep:{t.label}")) for t in DEMO_TRAINERS],),
        )
        say(f"{'trainer_attendance':24} {cur.fetchone()[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true")
    group.add_argument("--delete", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args()

    env = load_env()
    if args.create:
        create(env)
    elif args.delete:
        delete(env)
    else:
        status(env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
