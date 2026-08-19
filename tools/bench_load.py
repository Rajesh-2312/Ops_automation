"""Flood/measurement harness for the queries this app actually runs.

    python tools/bench_load.py --list
    python tools/bench_load.py --bench --scale 10
    python tools/bench_load.py --bench --scale 100 --json out/100x.json
    python tools/bench_load.py --explain home_attendance_month --persona sm
    python tools/bench_load.py --concurrency 4 --scale 100
    python tools/bench_load.py --sizes

WHERE THE QUERIES CAME FROM
===========================
Every statement in `QUERIES` is a transcription of a call site in this repo, and
each one carries the file and line it was read from. Nothing here is a query
somebody imagined the app might make. Two paths exist and they behave nothing
alike, so both are measured:

**The browser path (RLS).** `frontend/src/lib/supabase.ts` talks to PostgREST as
an authenticated user, so every row is filtered by the policies in
`supabase/migrations/`. PostgREST renders an embed (`select=*,trainers(...)`) as
a correlated subquery per parent row, which is transcribed faithfully below —
because the shape of the embed is exactly what decides whether a `SECURITY
DEFINER` helper is called once or once per row.

**The service path (BYPASSRLS).** `app/db/session.py` connects with a
service-role credential, so *no policy runs at all* and permission is enforced
in Python (`app/core/security.py`). `payout_queue_sequence` replays
`list_payout_queue` (`app/api/payouts.py:1159`) statement for statement.

The harness impersonates a persona the way PostgREST does — `set local role
authenticated` plus a `request.jwt.claims` GUC carrying the user's `sub`, which
is what `auth.uid()` reads (verified against the live `auth.uid()` body). That
measures the policy cost in the database without a JWT round trip in the way.

WHAT THE NUMBERS INCLUDE
------------------------
Wall clock from `execute()` to the last row fetched, over the real internet link
to the real Supabase instance. That includes one network round trip, which is
reported separately as `rtt_baseline` (`select 1`) so it can be subtracted. Rows
returned are reported too: a query that gets slower because it is returning
40,000 rows is a different defect from one that gets slower because it is
re-planning, and a latency column alone cannot tell them apart.

`p99` at fewer than 100 samples is the maximum observation. `n` is printed
beside every row so nobody reads more into it than it holds.

WHAT THIS HARNESS WILL NOT DO
-----------------------------
It never writes. `--bench` and `--explain` run inside a transaction that is
always rolled back, and `EXPLAIN ANALYZE` of a read is a read. Fixing anything
it finds is somebody else's decision (and somebody else's file).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("psycopg is not installed. Run inside the project venv.")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.load_data import (  # noqa: E402
    MANAGER_COLLEGES,
    load_env,
    load_id,
    plan_for,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: The month the dashboards land on. July 2026 keeps the benchmark in the same
#: month as CLAUDE.md §6's regression fixtures and `demo_data.py`.
MONTH_START: Final[dt.date] = dt.date(2026, 7, 1)
MONTH_END: Final[dt.date] = dt.date(2026, 7, 31)


# --- personas -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Persona:
    key: str
    label: str
    #: None means the service-role connection: BYPASSRLS, no policy evaluated.
    user_label: str | None

    @property
    def uid(self) -> uuid.UUID | None:
        return load_id(self.user_label) if self.user_label else None


PERSONAS: Final[dict[str, Persona]] = {
    "sm": Persona("sm", "Senior Manager (cluster reach)", "user:sm"),
    "mgr": Persona("mgr", "Manager (8 colleges)", "user:mgr"),
    "lde": Persona("lde", "LDE Executive (1 college)", "user:lde"),
    "service": Persona("service", "Service role (FastAPI, BYPASSRLS)", None),
}

INTERNAL_PERSONAS: Final[tuple[str, ...]] = ("sm", "mgr", "lde")


# --- the queries --------------------------------------------------------------
# PostgREST embed transcription note: `select=*,trainers(id,full_name)` becomes a
# scalar subquery in the target list, evaluated once per surviving parent row.
# That is why the embeds are written as subqueries here and not as plain joins —
# a join would let the planner hash it once and would understate the real cost.


@dataclass(frozen=True, slots=True)
class Query:
    key: str
    source: str
    sql: str
    params: tuple[Any, ...] = ()
    personas: tuple[str, ...] = INTERNAL_PERSONAS
    #: Cheap queries get more samples; expensive ones get a time budget.
    budget_s: float = 12.0
    max_n: int = 30
    note: str = ""


_DEPLOYMENTS_LIST = """
select d.id, d.trainer_id, d.batch_id, d.start_date, d.end_date,
       d.tracksheet_url, d.travel_notes, d.travel_submitted_at,
       d.created_at, d.updated_at,
       (select to_jsonb(x) from (
          select t.id, t.full_name, t.pan
          from public.trainers t where t.id = d.trainer_id) x) as trainers,
       (select to_jsonb(y) from (
          select b.id, b.name, b.passout_year,
                 (select to_jsonb(z) from (
                    select p.id, p.name, p.type,
                           (select to_jsonb(w) from (
                              select c.name from public.colleges c
                              where c.id = p.college_id) w) as colleges
                    from public.programs p where p.id = b.program_id) z) as programs
          from public.batches b where b.id = d.batch_id) y) as batches
from public.deployments d
order by d.start_date desc nulls last
"""

_PROGRAMS_LIST = """
select p.*,
       (select to_jsonb(x) from (
          select c.id, c.name from public.colleges c where c.id = p.college_id) x) as colleges
from public.programs p
order by p.created_at desc
"""

_OPEN_TASKS = """
select t.*,
       (select to_jsonb(x) from (
          select p.name,
                 (select to_jsonb(y) from (
                    select c.name from public.colleges c where c.id = p.college_id) y) as colleges
          from public.programs p where p.id = t.program_id) x) as programs
from public.tasks t
where t.status <> 'done'
order by t.due_date nulls last
"""

_BATCHES_LIST = """
select b.*,
       (select to_jsonb(x) from (
          select p.id, p.name, p.type,
                 (select to_jsonb(y) from (
                    select c.name from public.colleges c where c.id = p.college_id) y) as colleges
          from public.programs p where p.id = b.program_id) x) as programs
from public.batches b
order by b.name
"""

_WORK_ORDERS_LIST = """
select w.id, w.trainer_id, w.program_id, w.rate::text as rate, w.rate_basis,
       w.valid_from, w.valid_to, w.status, w.signed_at, w.document_url,
       (select to_jsonb(x) from (
          select t.full_name, t.pan from public.trainers t
           where t.id = w.trainer_id) x) as trainers,
       (select to_jsonb(y) from (
          select p.name, p.type,
                 (select to_jsonb(z) from (
                    select c.name from public.colleges c where c.id = p.college_id) z) as colleges
          from public.programs p where p.id = w.program_id) y) as programs
from public.work_orders w
order by w.valid_from desc
"""

_PNL_LIST = """
select l.id, l.program_id, l.revenue::text, l.trainer_cost::text,
       l.accrued_amount::text, l.invoiced_amount::text,
       (select to_jsonb(x) from (
          select p.name,
                 (select to_jsonb(y) from (
                    select c.name from public.colleges c where c.id = p.college_id) y) as colleges
          from public.programs p where p.id = l.program_id) x) as programs
from public.pnl l
"""


def build_queries(scale: int, a_deployment: str | None = None) -> dict[str, Query]:
    """The catalogue, parameterised by the deployment the AttendancePage opens.

    `a_deployment` is looked up live by `pick_deployment()` so the control query
    lands on a deployment that genuinely has July marks. Hard-coding
    `dep:0:0:0:0` returned zero rows — its window does not reach July — and a
    control that matches nothing measures the index probe and not the read.
    """
    a_deployment = a_deployment or str(load_id("dep:0:0:0:0"))
    return {
        q.key: q
        for q in (
            Query(
                "deployments_list",
                "DeploymentsPage.tsx:115 · HomePage.tsx:231 · AttendancePage.tsx:153 "
                "· PayoutsPage.tsx:192",
                _DEPLOYMENTS_LIST,
                budget_s=20.0,
                max_n=20,
                note="Four screens run this identical statement. No limit, no filter.",
            ),
            Query(
                "home_attendance_month",
                "HomePage.tsx:246",
                "select deployment_id, mark_date, mark from public.trainer_attendance "
                "where mark_date >= %s and mark_date <= %s",
                (MONTH_START, MONTH_END),
                budget_s=25.0,
                max_n=15,
                note="Whole month across every deployment in reach. The big one.",
            ),
            Query("programs_list", "HomePage.tsx:204 · ProgramConsole.tsx:54", _PROGRAMS_LIST),
            Query("open_tasks", "HomePage.tsx:217", _OPEN_TASKS, budget_s=20.0, max_n=20),
            Query(
                "trainers_list",
                "DeploymentsPage.tsx:125 · ErmSyncPage.tsx:964",
                "select * from public.trainers order by full_name",
                budget_s=20.0,
                max_n=20,
                note="LDE path goes through can_reach_trainer() per row.",
            ),
            Query(
                "batches_list",
                "DeploymentsPage.tsx:136 · HomePage.tsx:999",
                _BATCHES_LIST,
                budget_s=20.0,
                max_n=20,
            ),
            Query(
                "attendance_by_deployment_month",
                "AttendancePage.tsx:179",
                "select * from public.trainer_attendance "
                "where deployment_id = %s and mark_date >= %s and mark_date <= %s "
                "order by mark_date",
                (a_deployment, MONTH_START, MONTH_END),
                note="The one narrow, indexed read in the set. The control.",
            ),
            Query(
                "colleges_list",
                "CollegesPage.tsx:62",
                "select * from public.colleges order by name",
            ),
            Query(
                "work_orders_list",
                "HomePage.tsx:425 · HomePage.tsx:698",
                _WORK_ORDERS_LIST,
                personas=("sm", "mgr"),
                budget_s=20.0,
                max_n=20,
                note="Commercial. can_see_commercials() denies the LDE outright.",
            ),
            Query("pnl_list", "HomePage.tsx:409", _PNL_LIST, personas=("sm", "mgr")),
            Query(
                "students_count",
                "HomePage.tsx:1009",
                "select id, batch_id from public.students",
                budget_s=20.0,
                max_n=20,
            ),
            Query(
                "pending_approvals",
                "HomePage.tsx:397",
                "select id, artifact_type, artifact_id, version, state, submitted_at, "
                "created_at, notes from public.artifact_versions "
                "where state = 'PENDING_APPROVAL' order by submitted_at desc nulls last",
            ),
            Query(
                "rls_denied_pnl_lde",
                "R5 boundary · CLAUDE.md §4 commercials wall",
                # `select id`, not `select count(*)`: a count always returns one
                # row whatever the policy did, so it cannot demonstrate the wall.
                # Zero rows here IS the assertion.
                "select id from public.pnl",
                personas=("lde",),
                note="Must return 0 rows. Measured because a wall that is slow is still a cost.",
            ),
        )
    }


#: `list_payout_queue` (app/api/payouts.py:1159), statement for statement. Runs
#: on the service-role connection, so no policy is evaluated — the scoping is
#: the `college_ids` list, computed in Python from `principal.college_ids`.
PAYOUT_QUEUE_SEQUENCE: Final[tuple[tuple[str, str], ...]] = (
    ("colleges", "select * from public.colleges where id = any(%(colleges)s::uuid[])"),
    ("programs", "select * from public.programs where college_id = any(%(colleges)s::uuid[])"),
    ("batches", "select * from public.batches where program_id = any(%(programs)s::uuid[])"),
    ("deployments", "select * from public.deployments where batch_id = any(%(batches)s::uuid[])"),
    ("trainers", "select * from public.trainers where id = any(%(trainers)s::uuid[])"),
    (
        "attendance",
        "select * from public.trainer_attendance "
        "where deployment_id = any(%(deployments)s::uuid[]) "
        "and mark_date >= %(start)s and mark_date <= %(end)s",
    ),
    (
        "work_orders",
        "select * from public.work_orders where trainer_id = any(%(trainers)s::uuid[])",
    ),
    (
        "remuneration_sheets",
        "select * from public.remuneration_sheets where trainer_id = any(%(trainers)s::uuid[])",
    ),
)


# --- measurement --------------------------------------------------------------


@dataclass
class Sample:
    key: str
    persona: str
    rows: int = 0
    times_ms: list[float] = field(default_factory=list)
    #: Server-side `Execution Time` from EXPLAIN ANALYZE. The link to this
    #: Supabase costs ~100 ms per round trip and jitters by hundreds more, which
    #: at 1x is most of the wall clock. Without this column a reader cannot tell
    #: a query that got slower from a network that got worse.
    server_ms: list[float] = field(default_factory=list)
    error: str | None = None

    def pct(self, p: float) -> float:
        if not self.times_ms:
            return float("nan")
        ordered = sorted(self.times_ms)
        # Nearest-rank. At n < 100 the p99 IS the maximum, and saying so beats
        # interpolating a number that pretends to resolution it does not have.
        rank = max(1, min(len(ordered), int(-(-p * len(ordered) // 100))))
        return ordered[rank - 1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.key,
            "persona": self.persona,
            "n": len(self.times_ms),
            "rows": self.rows,
            "p50_ms": round(self.pct(50), 1) if self.times_ms else None,
            "p95_ms": round(self.pct(95), 1) if self.times_ms else None,
            "p99_ms": round(self.pct(99), 1) if self.times_ms else None,
            "min_ms": round(min(self.times_ms), 1) if self.times_ms else None,
            "max_ms": round(max(self.times_ms), 1) if self.times_ms else None,
            "mean_ms": round(statistics.fmean(self.times_ms), 1) if self.times_ms else None,
            "server_p50_ms": (
                round(statistics.median(self.server_ms), 1) if self.server_ms else None
            ),
            "server_max_ms": round(max(self.server_ms), 1) if self.server_ms else None,
            "error": self.error,
        }


def impersonate(cur: psycopg.Cursor, persona: Persona) -> None:
    """Become `persona` for the rest of this transaction.

    `set local` so it unwinds with the rollback, and `request.jwt.claims` because
    that is the GUC the live `auth.uid()` reads. Service role sets nothing: the
    connection already carries BYPASSRLS, which is exactly the FastAPI path.
    """
    if persona.uid is None:
        return
    cur.execute(
        "select set_config('request.jwt.claims', %s, true)",
        (json.dumps({"sub": str(persona.uid), "role": "authenticated"}),),
    )
    cur.execute("set local role authenticated")


def rtt_baseline(conn: psycopg.Connection, n: int = 20) -> float:
    """Median `select 1`. The floor under every number in the table."""
    times = []
    with conn.cursor() as cur:
        for _ in range(n):
            t = time.perf_counter()
            cur.execute("select 1")
            cur.fetchall()
            times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times)


def measure(conn: psycopg.Connection, query: Query, persona: Persona) -> Sample:
    sample = Sample(query.key, persona.key)
    deadline = time.perf_counter() + query.budget_s
    try:
        with conn.cursor() as cur:
            cur.execute("begin")
            impersonate(cur, persona)
            # Two warmups outside the sample: the first execution of a statement
            # pays parse and plan, and a cold shared_buffers read is a property
            # of the last thing that ran, not of this query.
            for _ in range(2):
                cur.execute(query.sql, query.params or None)
                cur.fetchall()
            while len(sample.times_ms) < query.max_n:
                t = time.perf_counter()
                cur.execute(query.sql, query.params or None)
                rows = cur.fetchall()
                sample.times_ms.append((time.perf_counter() - t) * 1000)
                sample.rows = len(rows)
                if time.perf_counter() > deadline and len(sample.times_ms) >= 5:
                    break
            # Server-side execution time, five reads, same transaction and same
            # persona. EXPLAIN ANALYZE of a SELECT executes the SELECT and
            # reports what the backend spent, with the link taken out.
            for _ in range(5):
                cur.execute(
                    f"explain (analyze, timing off, format json) {query.sql}",
                    query.params or None,
                )
                sample.server_ms.append(float(cur.fetchone()[0][0]["Execution Time"]))
    except Exception as exc:  # noqa: BLE001 - a failing query is a finding, not a crash
        sample.error = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        conn.rollback()
    return sample


def payout_queue_sample(conn: psycopg.Connection, scale: int, reps: int = 8) -> dict[str, Any]:
    """Replay `list_payout_queue` end to end on the service-role connection.

    Reported as the total of the statement sequence, because that total is what
    a Manager waits for when the Payouts page loads — the individual statements
    are only interesting when the total is bad.
    """
    plan = plan_for(scale)
    reach = [str(load_id(f"college:{i}")) for i in MANAGER_COLLEGES if i < plan.colleges]
    per_stmt: dict[str, list[float]] = {name: [] for name, _ in PAYOUT_QUEUE_SEQUENCE}
    totals: list[float] = []
    counts: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            for rep in range(reps + 1):
                bag: dict[str, Any] = {
                    "colleges": reach,
                    "programs": [],
                    "batches": [],
                    "deployments": [],
                    "trainers": [],
                    "start": MONTH_START,
                    "end": MONTH_END,
                }
                total = 0.0
                for name, sql in PAYOUT_QUEUE_SEQUENCE:
                    t = time.perf_counter()
                    cur.execute(sql, bag)
                    rows = cur.fetchall()
                    elapsed = (time.perf_counter() - t) * 1000
                    total += elapsed
                    if rep:  # rep 0 is the warmup
                        per_stmt[name].append(elapsed)
                    counts[name] = len(rows)
                    cols = [d.name for d in cur.description or []]
                    if name == "programs":
                        bag["programs"] = [str(r[cols.index("id")]) for r in rows]
                    elif name == "batches":
                        bag["batches"] = [str(r[cols.index("id")]) for r in rows]
                    elif name == "deployments":
                        bag["deployments"] = [str(r[cols.index("id")]) for r in rows]
                        bag["trainers"] = sorted({str(r[cols.index("trainer_id")]) for r in rows})
                if rep:
                    totals.append(total)
    finally:
        conn.rollback()
    ordered = sorted(totals)

    def pct(p: float) -> float:
        if not ordered:
            return float("nan")
        rank = max(1, min(len(ordered), int(-(-p * len(ordered) // 100))))
        return ordered[rank - 1]

    return {
        "reach_colleges": len(reach),
        "n": len(totals),
        "rows_per_statement": counts,
        "statement_p50_ms": {k: round(statistics.median(v), 1) for k, v in per_stmt.items() if v},
        "total_p50_ms": round(pct(50), 1),
        "total_p95_ms": round(pct(95), 1),
        "total_p99_ms": round(pct(99), 1),
    }


# --- reporting ----------------------------------------------------------------


def pick_deployment(conn: psycopg.Connection) -> str | None:
    """A load-test deployment that really has July 2026 marks, for the control.

    Chosen by row count so the control is a busy deployment, which is the case
    an LDE Executive actually opens.
    """
    with conn.cursor() as cur:
        # Constrained to college 0, which is the ONLY college all three personas
        # reach. An unconstrained pick returned a deployment outside the LDE's
        # single college, so the control measured an index probe that matched
        # nothing — a floor, not a read.
        cur.execute(
            "select a.deployment_id, count(*) from public.trainer_attendance a "
            "join public.deployments d on d.id = a.deployment_id "
            "join public.batches b on b.id = d.batch_id "
            "join public.programs p on p.id = b.program_id "
            "where p.college_id = %s and a.mark_date >= %s and a.mark_date <= %s "
            "group by 1 order by 2 desc limit 1",
            (str(load_id("college:0")), MONTH_START, MONTH_END),
        )
        row = cur.fetchone()
    conn.rollback()
    return str(row[0]) if row else None


def run_bench(env: dict[str, str], scale: int, out: Path | None) -> None:
    plan = plan_for(scale)
    print(f"\nBenchmark — scale {scale}x ({plan.colleges} colleges)\n" + "=" * 110)

    results: list[dict[str, Any]] = []
    with psycopg.connect(env["DATABASE_URL"], connect_timeout=60) as conn:
        conn.autocommit = False
        queries = build_queries(scale, pick_deployment(conn))
        rtt = rtt_baseline(conn)
        conn.rollback()
        print(f"rtt_baseline (median `select 1`): {rtt:.1f} ms")
        print(
            "wall = client-observed (includes the link). server = EXPLAIN ANALYZE Execution Time.\n"
        )
        print(
            f"{'query':32} {'persona':8} {'rows':>8} {'n':>4} "
            f"{'wall p50':>9} {'p95':>9} {'p99':>9} {'srv p50':>9} {'srv max':>9}"
        )
        print("-" * 110)
        for key, query in queries.items():
            for pkey in query.personas:
                sample = measure(conn, query, PERSONAS[pkey])
                d = sample.as_dict()
                results.append(d)
                if d["error"]:
                    print(f"{key:32} {pkey:8} {'ERROR':>8}  {d['error'][:56]}")
                    continue
                print(
                    f"{key:32} {pkey:8} {d['rows']:>8,} {d['n']:>4} "
                    f"{d['p50_ms']:>9.1f} {d['p95_ms']:>9.1f} {d['p99_ms']:>9.1f} "
                    f"{d['server_p50_ms']:>9.1f} {d['server_max_ms']:>9.1f}",
                    flush=True,
                )

        print("\nService-role path — list_payout_queue (app/api/payouts.py:1159)")
        print("-" * 110)
        pq = payout_queue_sample(conn, scale)
        print(f"  reach: {pq['reach_colleges']} colleges · n={pq['n']}")
        for name, ms in pq["statement_p50_ms"].items():
            print(f"    {name:22} p50 {ms:>9.1f} ms   rows {pq['rows_per_statement'][name]:>8,}")
        print(
            f"  TOTAL  p50 {pq['total_p50_ms']:.1f} ms · "
            f"p95 {pq['total_p95_ms']:.1f} ms · p99 {pq['total_p99_ms']:.1f} ms"
        )

        with conn.cursor() as cur:
            cur.execute("select pg_size_pretty(pg_database_size(current_database()))")
            size = cur.fetchone()[0]
        conn.rollback()

    payload = {
        "scale": scale,
        "colleges": plan.colleges,
        "rtt_baseline_ms": round(rtt, 1),
        "db_size": size,
        "queries": results,
        "payout_queue": pq,
        "measured_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {out}")


def run_explain(env: dict[str, str], query_key: str, persona_key: str, scale: int) -> None:
    queries = build_queries(scale)
    if query_key not in queries:
        sys.exit(f"unknown query {query_key!r}. --list to see them.")
    query = queries[query_key]
    persona = PERSONAS[persona_key]
    print(f"\nEXPLAIN (ANALYZE, BUFFERS) — {query_key} as {persona.label}")
    print(f"source: {query.source}\n" + "=" * 96)
    with psycopg.connect(env["DATABASE_URL"], connect_timeout=60) as conn, conn.cursor() as cur:
        cur.execute("begin")
        impersonate(cur, persona)
        # Warm once so the plan reported is the steady-state plan, not the first
        # parse. `EXPLAIN ANALYZE` of a SELECT executes the SELECT — a read.
        cur.execute(query.sql, query.params or None)
        cur.fetchall()
        cur.execute(
            f"explain (analyze, buffers, verbose false, timing on) {query.sql}",
            query.params or None,
        )
        for (line,) in cur.fetchall():
            print(line)
        conn.rollback()


def run_concurrency(env: dict[str, str], workers: int, scale: int, seconds: float) -> None:
    """N sessions running the HomePage query set at once.

    The dashboard is what everybody opens at 9am, so the interesting question is
    not "how slow is one query" but "what happens when eight people land on it
    together". Capped well under `max_connections`, which this instance reports
    as 60 and which the API server is also drawing from.
    """
    queries = build_queries(scale)
    home_set = ("programs_list", "open_tasks", "deployments_list", "home_attendance_month")
    personas = ["sm", "mgr", "lde"]
    lock = threading.Lock()
    per_query: dict[str, list[float]] = {k: [] for k in home_set}
    errors: list[str] = []

    def worker(idx: int) -> None:
        persona = PERSONAS[personas[idx % len(personas)]]
        try:
            with psycopg.connect(env["DATABASE_URL"], connect_timeout=60) as conn:
                deadline = time.perf_counter() + seconds
                while time.perf_counter() < deadline:
                    for key in home_set:
                        q = queries[key]
                        if persona.key not in q.personas:
                            continue
                        with conn.cursor() as cur:
                            cur.execute("begin")
                            impersonate(cur, persona)
                            t = time.perf_counter()
                            cur.execute(q.sql, q.params or None)
                            cur.fetchall()
                            ms = (time.perf_counter() - t) * 1000
                        conn.rollback()
                        with lock:
                            per_query[key].append(ms)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"worker {idx}: {type(exc).__name__}: {exc}"[:180])

    print(f"\nConcurrency — {workers} sessions, {seconds:.0f}s, HomePage query set\n" + "=" * 96)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started

    print(f"{'query':32} {'n':>6} {'p50':>10} {'p95':>10} {'p99':>10} {'max':>10}")
    print("-" * 96)
    total = 0
    for key, times in per_query.items():
        if not times:
            continue
        total += len(times)
        ordered = sorted(times)

        def at(p: float, o: list[float] = ordered) -> float:
            return o[max(1, min(len(o), int(-(-p * len(o) // 100)))) - 1]

        print(
            f"{key:32} {len(times):>6} {at(50):>10.1f} {at(95):>10.1f} "
            f"{at(99):>10.1f} {max(ordered):>10.1f}"
        )
    print(f"\nthroughput: {total / elapsed:.1f} queries/sec across {workers} sessions")
    for e in errors:
        print(f"  ERROR {e}")


def run_sizes(env: dict[str, str]) -> None:
    """Table and index sizes, plus the seq-scan / index-scan counters.

    `pg_stat_user_tables` is cumulative since the last stats reset, so a large
    `seq_scan` here is evidence a plan chose one, not proof of when.
    """
    with psycopg.connect(env["DATABASE_URL"], connect_timeout=60) as conn, conn.cursor() as cur:
        print("\nTable sizes\n" + "=" * 96)
        cur.execute(
            "select relname, n_live_tup, seq_scan, seq_tup_read, idx_scan, "
            "pg_size_pretty(pg_total_relation_size(relid)) "
            "from pg_stat_user_tables where schemaname = 'public' "
            "order by pg_total_relation_size(relid) desc limit 20"
        )
        print(
            f"{'table':28} {'live rows':>12} {'seq_scan':>10} {'seq_rows':>14} "
            f"{'idx_scan':>10} {'total':>10}"
        )
        for name, live, seq, seqr, idx, size in cur.fetchall():
            print(
                f"{name:28} {live:>12,} {seq or 0:>10,} {seqr or 0:>14,} "
                f"{idx or 0:>10,} {size:>10}"
            )
        print("\nIndex usage on the big tables\n" + "=" * 96)
        cur.execute(
            "select relname, indexrelname, idx_scan, "
            "pg_size_pretty(pg_relation_size(indexrelid)) "
            "from pg_stat_user_indexes where schemaname = 'public' "
            "and relname in ('trainer_attendance','deployments','trainers','programs',"
            "'batches','tasks','students','work_orders') "
            "order by relname, indexrelname"
        )
        print(f"{'table':24} {'index':44} {'scans':>10} {'size':>10}")
        for rel, idx_name, scans, size in cur.fetchall():
            print(f"{rel:24} {idx_name:44} {scans or 0:>10,} {size:>10}")
        conn.rollback()


def list_queries(scale: int) -> None:
    print(f"{'key':32} {'personas':22} source")
    print("-" * 110)
    for key, q in build_queries(scale).items():
        print(f"{key:32} {','.join(q.personas):22} {q.source}")
        if q.note:
            print(f"{'':32} {'':22} -- {q.note}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bench", action="store_true")
    group.add_argument("--explain", metavar="QUERY_KEY")
    group.add_argument("--concurrency", type=int, metavar="N")
    group.add_argument("--sizes", action="store_true")
    group.add_argument("--list", action="store_true")
    parser.add_argument("--scale", type=int, default=10)
    parser.add_argument("--persona", default="sm", choices=sorted(PERSONAS))
    parser.add_argument("--seconds", type=float, default=25.0)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    if args.list:
        list_queries(args.scale)
        return 0

    env = load_env()
    if args.bench:
        run_bench(env, args.scale, args.json)
    elif args.explain:
        run_explain(env, args.explain, args.persona, args.scale)
    elif args.concurrency:
        run_concurrency(env, args.concurrency, args.scale, args.seconds)
    else:
        run_sizes(env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
