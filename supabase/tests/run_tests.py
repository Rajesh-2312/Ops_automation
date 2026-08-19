#!/usr/bin/env python3
"""Apply the byteXL Ops migrations and assert the CLAUDE.md §4 persona matrix.

    python supabase/tests/run_tests.py              # apply, seed, then assert
    python supabase/tests/run_tests.py --apply-only # apply and seed, no assertions

Reads DATABASE_URL from the repo-root .env, never from the command line, so the
password does not land in shell history. Use Supabase's DIRECT or SESSION POOLER
connection string. The transaction pooler on port 6543 is refused outright --
see the note in connect().

WHAT GETS COMMITTED
    Migrations and supabase/seed.sql. These are the deliverable. Each migration
    is recorded in supabase_migrations.schema_migrations -- the same table the
    Supabase CLI uses -- so re-running skips what is already applied and a later
    `supabase db push` agrees with this script.

WHAT NEVER GETS COMMITTED
    Isolation, fixtures and assertions run inside ONE transaction that is always
    rolled back: on pass, on failure, and on Ctrl-C. The eleven fake auth.users
    rows, the colleges, the fake P&L numbers, the `test` schema: none of it
    survives. That is what makes this safe to point at a project with data in it.

WHY THE ISOLATION STEP EXISTS
    00_isolate.sql deletes every application row as the first statement of that
    same transaction. Without it, `select count(*) from public.batches` would
    only ever prove "the rows I already knew about are visible" -- it would pass
    even if the policies leaked every other row in the database. With it, the
    counts are unscoped, and an unscoped count is the only kind that proves a
    negative. Other sessions never observe the delete: under MVCC they keep
    reading the pre-transaction snapshot throughout.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    import psycopg
except ImportError:  # pragma: no cover - environment guard
    sys.exit("psycopg is not installed. Run:\n" '    python -m pip install "psycopg[binary]"')

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPABASE_DIR = REPO_ROOT / "supabase"
MIGRATIONS_DIR = SUPABASE_DIR / "migrations"
TESTS_DIR = SUPABASE_DIR / "tests"
SEED_FILE = SUPABASE_DIR / "seed.sql"

ISOLATE_FILE = TESTS_DIR / "00_isolate.sql"
FIXTURES_FILE = TESTS_DIR / "01_fixtures.sql"
MATRIX_FILE = TESTS_DIR / "02_rls_matrix_test.sql"

# Same table the Supabase CLI uses, so this script and `supabase db push` share
# one view of what has been applied.
MIGRATION_TABLE_DDL = """
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
    version     text primary key,
    statements  text[],
    name        text
);
"""


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


class Style:
    CYAN = "\033[36m"
    GREY = "\033[90m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    BOLD = "\033[1m"
    OFF = "\033[0m"


def step(msg: str) -> None:
    print(f"\n{Style.CYAN}>>> {msg}{Style.OFF}", flush=True)


def detail(msg: str) -> None:
    print(f"    {Style.GREY}{msg}{Style.OFF}", flush=True)


def warn(msg: str) -> None:
    print(f"{Style.YELLOW}!!! {msg}{Style.OFF}", flush=True)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def load_database_url() -> str:
    """Read DATABASE_URL out of .env without pulling in python-dotenv.

    Deliberately not importing app.core.config: this script must run before the
    application package is installable, and it needs exactly one variable.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        sys.exit(
            f"No .env found at {env_path}\n\n"
            "Copy .env.example to .env and set DATABASE_URL to your Supabase\n"
            "connection string (Dashboard -> Project Settings -> Database ->\n"
            "Connection string -> URI). Do not use the anon or service_role API\n"
            "key: this script needs SET ROLE, which only a direct Postgres\n"
            "connection can do."
        )

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "DATABASE_URL":
            continue
        value = value.strip().strip('"').strip("'")
        if not value or value.startswith("postgresql://[") or "YOUR-PASSWORD" in value:
            sys.exit("DATABASE_URL in .env is still the placeholder. Fill it in.")
        return value

    sys.exit("DATABASE_URL is not set in .env")


def redact(url: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:****@", url)


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


def on_notice(diag: psycopg.errors.Diagnostic) -> None:
    """Stream RAISE NOTICE output -- that is where every assertion reports."""
    message = (diag.message_primary or "").rstrip()
    if not message:
        print(flush=True)
        return
    if message.startswith("  ok"):
        print(f"    {Style.GREEN}{message.strip()}{Style.OFF}", flush=True)
    elif message.startswith("==="):
        print(f"\n{Style.BOLD}{message}{Style.OFF}", flush=True)
    elif message.startswith("---"):
        print(f"  {Style.GREY}{message}{Style.OFF}", flush=True)
    else:
        print(f"    {message}", flush=True)


def connect(url: str) -> psycopg.Connection:
    parts = urlsplit(url)

    # THE 6543 REFUSAL. Supabase's transaction pooler multiplexes statements
    # across backends and does not preserve SET ROLE or session GUCs between
    # them. Every `set role authenticated` in 02_rls_matrix_test.sql would be
    # silently dropped, leaving the assertions running as the BYPASSRLS owner --
    # so all of them would pass, and none of them would mean anything. A test
    # suite that passes for the wrong reason is worse than no suite, so this
    # exits rather than warns. app/core/config.py rejects the same port.
    if parts.port == 6543:
        warn(
            "Port 6543 is Supabase's TRANSACTION pooler. It does not preserve\n"
            "    SET ROLE or session settings between statements, so every RLS\n"
            "    assertion would run as the owner and pass while proving nothing.\n"
            "    Use the direct connection or the session pooler (port 5432)."
        )
        sys.exit(1)

    try:
        conn = psycopg.connect(url, autocommit=False, connect_timeout=20)
    except psycopg.OperationalError as exc:
        text = str(exc)
        hint = ""
        if "Network is unreachable" in text or "could not translate" in text:
            hint = (
                "\n\nSupabase's direct connection host is IPv6-only on newer\n"
                "projects. If this machine has no IPv6, use the *Session pooler*\n"
                "URI from the same dashboard page instead (it is IPv4)."
            )
        sys.exit(f"Could not connect: {text}{hint}")

    conn.add_notice_handler(on_notice)
    return conn


def preflight(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("select version()")
        version = cur.fetchone()[0].split(",")[0]
        cur.execute(
            "select current_user, current_database(), "
            "       (select rolsuper from pg_roles where rolname = current_user), "
            "       (select rolbypassrls from pg_roles where rolname = current_user)"
        )
        user, db, is_super, bypass_rls = cur.fetchone()
    conn.rollback()

    detail(version)
    detail(f"database={db}  user={user}  superuser={is_super}  bypassrls={bypass_rls}")

    # Every table is created with FORCE ROW LEVEL SECURITY, which subjects even
    # the table owner to policy. Migrations and fixtures only work because the
    # connecting role bypasses RLS outright.
    if not (is_super or bypass_rls):
        warn(
            f"Role '{user}' has neither SUPERUSER nor BYPASSRLS. Every table in\n"
            "    this schema uses FORCE ROW LEVEL SECURITY, so migrations and\n"
            "    fixtures will be blocked by their own policies. Connect as the\n"
            "    'postgres' role from the dashboard connection string."
        )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    """Execute a whole file in one round trip.

    psycopg uses the simple query protocol when no parameters are passed, which
    is what allows multiple statements per execute -- and lets the server do the
    parsing, so dollar-quoted function bodies need no special handling here.
    """
    if not path.exists():
        sys.exit(f"Missing SQL file: {path}")
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)


def applied_versions(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(MIGRATION_TABLE_DDL)
        cur.execute("select version from supabase_migrations.schema_migrations")
        rows = cur.fetchall()
    conn.commit()
    return {r[0] for r in rows}


def apply_migrations(conn: psycopg.Connection) -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        sys.exit(f"No migrations found in {MIGRATIONS_DIR}")

    already = applied_versions(conn)
    applied = 0

    for path in files:
        version = path.stem.split("_", 1)[0]
        name = path.stem.split("_", 1)[1] if "_" in path.stem else path.stem
        if version in already:
            detail(f"skip  {path.name} (already applied)")
            continue

        try:
            run_sql_file(conn, path)
            with conn.cursor() as cur:
                cur.execute(
                    "insert into supabase_migrations.schema_migrations (version, name) "
                    "values (%s, %s) on conflict (version) do nothing",
                    (version, name),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            print(f"\n{Style.RED}Migration failed: {path.name}{Style.OFF}")
            raise

        detail(f"apply {path.name}")
        applied += 1

    return applied


def apply_seed(conn: psycopg.Connection) -> None:
    run_sql_file(conn, SEED_FILE)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "select (select count(*) from public.task_templates), "
            "       (select count(distinct stage) from public.task_templates), "
            "       (select count(*) from public.document_templates), "
            "       (select count(distinct category) from public.document_templates)"
        )
        tasks, stages, docs, categories = cur.fetchone()
    conn.commit()
    detail(f"{tasks} task templates across {stages} stages")
    detail(f"{docs} document templates across {categories} categories")


def run_assertions(conn: psycopg.Connection) -> None:
    """Isolate + fixtures + assertions in one transaction, always rolled back.

    The `finally` is the whole safety story: the rollback happens whether the
    assertions passed, raised, or the user hit Ctrl-C mid-run. Nothing here is
    ever committed, including the isolation delete that makes the counts
    meaningful in the first place.
    """
    try:
        run_sql_file(conn, ISOLATE_FILE)
        run_sql_file(conn, FIXTURES_FILE)
        run_sql_file(conn, MATRIX_FILE)
    finally:
        conn.rollback()


# --------------------------------------------------------------------------- #


def main() -> int:
    # Before parse_args: argparse prints --help and exits from inside it, and
    # this docstring contains a section sign that a cp1252 console cannot encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply-only",
        action="store_true",
        help="apply migrations + seed and stop; skip fixtures and assertions",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="connect and report server/role details, apply nothing",
    )
    parser.add_argument(
        "--keep-fixtures",
        action="store_true",
        help="(refused) would commit the isolation delete -- see run_assertions",
    )
    args = parser.parse_args()

    if args.keep_fixtures:
        # Committing the fixtures would also commit 00_isolate.sql's deletion of
        # every application row. Refuse rather than destroy data.
        sys.exit(
            "--keep-fixtures cannot be combined with the isolation step: committing\n"
            "the fixtures would also commit the deletion of every existing\n"
            "application row. Remove the flag."
        )

    url = load_database_url()
    step("Connecting")
    detail(redact(url))

    conn = connect(url)
    try:
        preflight(conn)

        if args.dry_run:
            print(f"\n{Style.GREEN}Dry run only -- nothing applied.{Style.OFF}\n")
            return 0

        step("Applying migrations")
        count = apply_migrations(conn)
        detail(f"{count} migration(s) applied this run")

        step("Seeding template libraries")
        apply_seed(conn)

        if args.apply_only:
            print(
                f"\n{Style.GREEN}Schema applied and seeded. Assertions skipped "
                f"(--apply-only).{Style.OFF}\n"
            )
            return 0

        step("Asserting the CLAUDE.md section 4 persona matrix")
        detail("isolation + fixtures + assertions run in a transaction that is always rolled back")
        run_assertions(conn)

        print(
            f"\n{Style.GREEN}{Style.BOLD}PASS{Style.OFF}"
            f"{Style.GREEN} -- schema applied and every persona assertion held.{Style.OFF}"
        )
        print(f"{Style.GREY}     Fixture data rolled back; nothing left behind.{Style.OFF}\n")
        return 0

    except psycopg.Error as exc:
        print(f"\n{Style.RED}{Style.BOLD}FAIL{Style.OFF}{Style.RED} -- {exc}{Style.OFF}\n")
        return 1
    except KeyboardInterrupt:
        conn.rollback()
        print(f"\n{Style.YELLOW}Interrupted; transaction rolled back.{Style.OFF}\n")
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
