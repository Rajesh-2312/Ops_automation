"""Exercise and benchmark every registered API route.

    python tools/bench_api.py                      # in-process, read-only sweep
    python tools/bench_api.py --iterations 20
    python tools/bench_api.py --allow-writes       # add the mutating cases
    python tools/bench_api.py --include-llm        # add the OpenRouter-backed cases
    python tools/bench_api.py --base-url http://127.0.0.1:8000
    python tools/bench_api.py --json out.json --markdown out.md

WHAT THIS IS FOR
================
Every screen in `frontend/` was written by READING `app/api/*.py`. Nothing had
ever performed a round trip. This harness performs the round trip: it calls each
route with a real Supabase-shaped JWT against the real database, records the
status and the response body, checks the body against the route's declared
`response_model`, and times it.

WHY A SELECTOR EVENT LOOP (READ BEFORE "SIMPLIFYING" `_client`)
==============================================================
`fastapi.testclient.TestClient` runs the app through anyio, which on Windows
builds a `ProactorEventLoop`, and psycopg's async driver refuses to run on one.
The failure is the exact partial shape `run_api.py`'s docstring warns about:
`/health` returns 200 because it touches no database, and every other route
raises `psycopg.InterfaceError` as a 500. A harness that reported that as "31
endpoints are broken" would be reporting its own bug.

anyio passes `backend_options["loop_factory"]` straight to `asyncio.Runner`, so
the same selector loop `run_api.py` builds for uvicorn is built here for the
test client. On Linux and macOS this changes nothing.

WHAT IT WILL AND WILL NOT WRITE
===============================
Read-only by default. `--allow-writes` adds the mutating cases, and every one of
them either

  * replays an existing row (`POST /payouts/commit` against a period already
    committed returns 200 `created=false` and touches no column), or
  * is generation that is idempotent by template id and already saturated, or
  * creates rows this process then deletes by primary key, tracked in
    `_Created`, in a `finally`.

Nothing is deleted that this process did not create. `--allow-writes` never
touches `remuneration_sheets`: a payout row carries an invoice number under a
unique constraint and issuing one for a benchmark would burn a real sequence.

LATENCY, AND WHY LLM ROUTES ARE REPORTED SEPARATELY
===================================================
Two of these routes can call OpenRouter (`POST /copilot/ask`, and the reporting
routes when `include_narrative`/`include_trainer_cost` ask for prose). Those are
network-bound on a third party and their p99 says nothing about this codebase.
They are tagged `llm`, excluded unless `--include-llm`, and reported in their own
table so a 4-second p95 is never averaged into a database number.

`cold_ms` is the first call of a case — connection establishment, statement
preparation, Pydantic model construction. The percentiles are over the warm
calls that follow it. The gap between them is the interesting number.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import selectors
import statistics
import sys
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# `tools/` is not a package and this file is run as a script, so the repo root
# has to be importable before `app.*` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from jose import jwt  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


# --- personas -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Actor:
    """A caller: a profile id, the persona it holds and what it reaches.

    Resolved from the database rather than invented, because `resolve_principal`
    reads `profiles` and the two assignment tables on every request — a token for
    a user with no profile row is a 401 and would benchmark nothing.
    """

    label: str
    user_id: str | None
    persona: str
    note: str


#: Minted per actor, cached for the run. Supabase's own tokens are ES256 against
#: JWKS on a migrated project and HS256 against the shared secret otherwise;
#: `app/core/security.verify_access_token` accepts both and picks by header, so
#: HS256 is the one a harness can produce without a browser session.
_tokens: dict[str, str] = {}


def token_for(actor: Actor, *, ttl_seconds: int = 3600) -> str | None:
    if actor.user_id is None:
        return None
    if actor.label in _tokens:
        return _tokens[actor.label]
    secret = os.environ.get("SUPABASE_JWT_SECRET", "")
    if not secret:
        raise SystemExit("SUPABASE_JWT_SECRET is not set; no caller can be authenticated.")
    claims = {
        "sub": actor.user_id,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + ttl_seconds,
        "iat": int(time.time()),
    }
    _tokens[actor.label] = jwt.encode(claims, secret, algorithm="HS256")
    return _tokens[actor.label]


def discover_actors(dsn: str) -> dict[str, Actor]:
    """Pick one profile per persona, preferring one that actually reaches rows.

    A Manager with no `user_college_assignments` row reaches nothing and every
    scoped endpoint answers empty — correct behaviour, useless for benchmarking.
    Both are wanted, so both are selected and labelled.
    """
    actors: dict[str, Actor] = {
        "anon": Actor("anon", None, "-", "no Authorization header"),
    }
    with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("""
            select p.id, p.role, p.full_name,
                   (select count(*) from user_college_assignments u where u.user_id = p.id)
                 + (select count(*) from user_cluster_assignments c where c.user_id = p.id)
            from profiles p order by p.role, 4 desc
            """)
        rows = cur.fetchall()

    def pick(role: str, *, with_reach: bool) -> Actor | None:
        for uid, r, name, reach in rows:
            if r == role and (reach > 0) == with_reach:
                return Actor(
                    f"{role}{'' if with_reach else '_no_reach'}",
                    str(uid),
                    role,
                    f"{name} (assignments={reach})",
                )
        return None

    for role in ("senior_manager", "manager", "lde_executive", "trainer", "college"):
        for with_reach in (True, False):
            a = pick(role, with_reach=with_reach)
            if a is not None:
                actors[a.label] = a
    return actors


# --- fixtures resolved from the database --------------------------------------


@dataclass
class Fixtures:
    """Real ids, so a 404 in the report means a bug and not a made-up UUID."""

    program_id: str = ""
    program_id_out_of_reach: str = ""
    college_id: str = ""
    #: bCAP, rate 80000/month — drives the CLAUDE.md §6 VEMA PRUDHVI SAI fixture
    #: (26–31 Jul 2026, TA&DA 100, net 14,035).
    deployment_80k: str = ""
    #: bCAP, rate 65000/month — drives the Bushily Kondala Rao fixture
    #: (full Jul 2026, net 58,500). Already committed, so commit replays.
    deployment_65k: str = ""
    #: per_day, rate 3500 — the only CRT-shaped engagement on file.
    deployment_per_day: str = ""
    #: A deployment whose attendance is short of the period (bCAP warning path).
    deployment_incomplete: str = ""
    artifact_id: str = ""
    artifact_type: str = "remuneration_sheets"
    assignee_id: str = ""
    trainer_id: str = ""

    missing_uuid: str = "00000000-0000-4000-8000-000000000000"


def discover_fixtures(dsn: str, actors: dict[str, Actor]) -> Fixtures:
    """Anchor everything on the two §6 payout deployments, then take scope from them.

    Order matters. Picking a program first and hoping a matching engagement lives
    under it produces a fixture set where the §6 assertions cannot run — which is
    how a harness ends up reporting a green sweep that never touched the payout
    arithmetic. So the ₹80,000 and ₹65,000 per-month engagements with July 2026
    marks are found first, and the college, program and personas are derived from
    whichever program carries them.
    """
    f = Fixtures()
    with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("""
            select d.id, w.rate, w.rate_basis, b.program_id, p.college_id,
                   (select count(*) from trainer_attendance a
                     where a.deployment_id = d.id
                       and a.mark_date between date '2026-07-01' and date '2026-07-31')
            from deployments d
            join batches b on b.id = d.batch_id
            join programs p on p.id = b.program_id
            join work_orders w on w.trainer_id = d.trainer_id and w.program_id = b.program_id
            order by w.rate desc
            """)
        rows = cur.fetchall()
        for did, rate, basis, program_id, college_id, marks in rows:
            if (
                basis == "per_month"
                and Decimal(rate) == Decimal("65000.00")
                and marks >= 28
                and not f.deployment_65k
            ):
                f.deployment_65k = str(did)
                f.program_id, f.college_id = str(program_id), str(college_id)
        for did, rate, basis, program_id, _college, marks in rows:
            if str(program_id) != f.program_id:
                continue
            if (
                basis == "per_month"
                and Decimal(rate) == Decimal("80000.00")
                and not f.deployment_80k
            ):
                f.deployment_80k = str(did)
            if basis == "per_day" and not f.deployment_per_day:
                f.deployment_per_day = str(did)
            if 0 < marks < 31 and not f.deployment_incomplete:
                f.deployment_incomplete = str(did)
        # Fall back to anything at all rather than leaving a case unrunnable.
        for did, rate, basis, _prog, _college, marks in rows:
            if (
                basis == "per_month"
                and Decimal(rate) == Decimal("80000.00")
                and not f.deployment_80k
            ):
                f.deployment_80k = str(did)
            if basis == "per_day" and not f.deployment_per_day:
                f.deployment_per_day = str(did)
            if 0 < marks < 31 and not f.deployment_incomplete:
                f.deployment_incomplete = str(did)

        cur.execute(
            "select id from programs where college_id is distinct from %s limit 1",
            (f.college_id or None,),
        )
        row = cur.fetchone()
        if row:
            f.program_id_out_of_reach = str(row[0])

        cur.execute("select artifact_type, artifact_id from artifact_versions limit 1")
        row = cur.fetchone()
        if row:
            f.artifact_type, f.artifact_id = str(row[0]), str(row[1])

        # A trainer inside the fixture college, so the ERM flow authorises.
        cur.execute(
            """
            select distinct d.trainer_id from deployments d
            join batches b on b.id = d.batch_id
            join programs p on p.id = b.program_id
            where p.college_id = %s limit 1
            """,
            (f.college_id or None,),
        )
        row = cur.fetchone()
        if row:
            f.trainer_id = str(row[0])
        else:
            cur.execute("select id from trainers limit 1")
            row = cur.fetchone()
            if row:
                f.trainer_id = str(row[0])

    f.assignee_id = actors["manager"].user_id if "manager" in actors else ""
    return f


def prefer_reaching_actors(dsn: str, actors: dict[str, Actor], college_id: str) -> None:
    """Re-point each scoped persona at a profile that actually reaches the fixture.

    `discover_actors` takes the first profile per persona with any assignment at
    all, which is not necessarily an assignment to the college the fixtures were
    drawn from. A Senior Manager assigned to a different cluster is a perfectly
    correct 403 — and benchmarking the approvals endpoints against one would
    report "403" for every case and prove nothing about the endpoint.
    """
    if not college_id:
        return
    with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute(
            """
            select p.id, p.role, p.full_name
            from profiles p
            where exists (select 1 from user_college_assignments u
                          where u.user_id = p.id and u.college_id = %s)
               or exists (select 1 from user_cluster_assignments uc
                          join colleges c on c.cluster_id = uc.cluster_id
                          where uc.user_id = p.id and c.id = %s)
            """,
            (college_id, college_id),
        )
        for uid, role, name in cur.fetchall():
            if role in actors and actors[role].user_id != str(uid) or role not in actors:
                actors[role] = Actor(role, str(uid), role, f"{name} (reaches fixture college)")


# --- the case table -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One request, repeated, with what a correct answer looks like."""

    name: str
    method: str
    path: str
    actor: str = "manager"
    body: Any = None
    #: Statuses that are a correct answer. A status outside this set is a
    #: finding, and 500 is never in it.
    expect: tuple[int, ...] = (200,)
    tags: tuple[str, ...] = ()
    #: Checked against the decoded JSON when the call succeeds.
    assert_json: Any = None
    note: str = ""

    @property
    def endpoint(self) -> str:
        return f"{self.method} {self.path.split('?')[0]}"


JUL = ("2026-07-01", "2026-07-31")


def build_cases(f: Fixtures, actors: dict[str, Actor]) -> list[Case]:
    """Every registered route, plus the auth, error and absurd-value probes."""
    have_sm = "senior_manager" in actors
    sm = "senior_manager" if have_sm else "manager"
    cases: list[Case] = []
    add = cases.append

    # --- health ---------------------------------------------------------------
    add(Case("health", "GET", "/health", actor="anon", tags=("nodb",)))

    # --- copilot --------------------------------------------------------------
    add(Case("copilot.corpora", "GET", "/copilot/corpora"))
    add(Case("copilot.corpora.anon", "GET", "/copilot/corpora", actor="anon", expect=(401,)))
    add(Case("copilot.corpora.lde", "GET", "/copilot/corpora", actor="lde_executive"))
    add(
        Case(
            "copilot.ask",
            "POST",
            "/copilot/ask",
            body={"question": "What is the attendance completeness rule for CRT?"},
            expect=(200, 503),
            tags=("llm",),
        )
    )
    add(
        Case(
            "copilot.ask.badlimit",
            "POST",
            "/copilot/ask",
            body={"question": "What is the SOP?", "limit": 9999},
            expect=(422,),
        )
    )
    add(
        Case(
            "copilot.ask.short",
            "POST",
            "/copilot/ask",
            body={"question": "x"},
            expect=(422,),
        )
    )
    add(
        Case(
            "copilot.ask.extra_field",
            "POST",
            "/copilot/ask",
            body={"question": "What is the SOP?", "facts": {"net": "1"}},
            expect=(422,),
            note="extra='forbid' — a client-supplied 'fact' would invert R1",
        )
    )

    # --- monitoring -----------------------------------------------------------
    add(Case("monitoring.rules", "GET", "/monitoring/rules"))
    add(Case("monitoring.rules.anon", "GET", "/monitoring/rules", actor="anon", expect=(401,)))
    add(Case("monitoring.alerts", "GET", "/monitoring/alerts"))
    add(
        Case(
            "monitoring.alerts.program",
            "GET",
            f"/monitoring/alerts?program_id={f.program_id}",
        )
    )
    add(
        Case(
            "monitoring.alerts.baduuid",
            "GET",
            "/monitoring/alerts?program_id=not-a-uuid",
            expect=(422,),
        )
    )
    add(
        Case(
            "monitoring.alerts.farfuture",
            "GET",
            "/monitoring/alerts?period_start=2999-01-01&period_end=2999-12-31",
            expect=(200, 422),
        )
    )
    add(
        Case(
            "monitoring.alerts.reversed",
            "GET",
            "/monitoring/alerts?period_start=2026-12-31&period_end=2026-01-01",
            expect=(200, 422),
            note="period_end precedes period_start",
        )
    )

    # --- payouts --------------------------------------------------------------
    add(Case("payouts.queue", "GET", "/payouts?month=2026-07"))
    add(Case("payouts.queue.sm", "GET", "/payouts?month=2026-07", actor=sm))
    add(
        Case(
            "payouts.queue.lde",
            "GET",
            "/payouts?month=2026-07",
            actor="lde_executive",
            expect=(403,),
            note="commercials wall (§4)",
        )
    )
    add(Case("payouts.queue.anon", "GET", "/payouts?month=2026-07", actor="anon", expect=(401,)))
    add(Case("payouts.queue.badmonth", "GET", "/payouts?month=2026-13", expect=(422,)))
    add(Case("payouts.queue.nomonth", "GET", "/payouts", expect=(422,)))
    add(Case("payouts.queue.absurdmonth", "GET", "/payouts?month=9999-12", expect=(200,)))

    preview_80k = {
        "deployment_id": f.deployment_80k,
        "period_start": "2026-07-26",
        "period_end": "2026-07-31",
        "ta_da": "100",
    }
    preview_65k = {
        "deployment_id": f.deployment_65k,
        "period_start": JUL[0],
        "period_end": JUL[1],
    }
    add(
        Case(
            "payouts.preview.fixture_14035",
            "POST",
            "/payouts/preview",
            body=preview_80k,
            # §6 quotes the sheet's DISPLAY figures (earned 15,484 · gross 15,584 ·
            # tds 1,548 · net 14,035). Only `net` is rounded by the engine (R6), so
            # only `net` is asserted as an exact string; the intermediates arrive at
            # full Decimal precision and are checked to the rupee in
            # tests/api_contract/test_payout_fixtures.py.
            assert_json={
                "breakdown.net": "14035",
                "breakdown.rate_basis": "per_month",
                "breakdown.payable_days": "6",
                "attendance.period_days": 6,
            },
            note="CLAUDE.md §6 fixture 1 (VEMA PRUDHVI SAI shape)",
        )
    )
    add(
        Case(
            "payouts.preview.fixture_58500",
            "POST",
            "/payouts/preview",
            body=preview_65k,
            assert_json={"breakdown.net": "58500", "breakdown.payable_days": "31"},
            note="CLAUDE.md §6 fixture 2 (Bushily Kondala Rao shape)",
        )
    )
    add(
        Case(
            "payouts.preview.lde",
            "POST",
            "/payouts/preview",
            body=preview_65k,
            actor="lde_executive",
            expect=(403,),
        )
    )
    add(
        Case(
            "payouts.preview.anon",
            "POST",
            "/payouts/preview",
            body=preview_65k,
            actor="anon",
            expect=(401,),
        )
    )
    add(
        Case(
            "payouts.preview.no_reach",
            "POST",
            "/payouts/preview",
            body=preview_65k,
            actor="manager_no_reach",
            expect=(403,),
        )
    )
    add(
        Case(
            "payouts.preview.missing_deployment",
            "POST",
            "/payouts/preview",
            body={**preview_65k, "deployment_id": f.missing_uuid},
            expect=(404,),
        )
    )
    add(
        Case(
            "payouts.preview.float_money",
            "POST",
            "/payouts/preview",
            body={**preview_65k, "ta_da": 100.5},
            expect=(422,),
            note="R7 — a JSON float must never widen into a Decimal",
        )
    )
    add(
        Case(
            "payouts.preview.spans_two_months",
            "POST",
            "/payouts/preview",
            body={**preview_65k, "period_end": "2026-08-05"},
            expect=(422,),
        )
    )
    add(
        Case(
            "payouts.preview.reversed_period",
            "POST",
            "/payouts/preview",
            body={
                "deployment_id": f.deployment_65k,
                "period_start": "2026-07-31",
                "period_end": "2026-07-01",
            },
            expect=(422,),
        )
    )
    add(
        Case(
            "payouts.preview.rate_without_basis",
            "POST",
            "/payouts/preview",
            body={**preview_65k, "rate": "1000"},
            expect=(422,),
        )
    )
    add(
        Case(
            "payouts.preview.unknown_field",
            "POST",
            "/payouts/preview",
            body={**preview_65k, "net": "999999"},
            expect=(422,),
            note="extra='forbid' — a caller-supplied figure must not reach a column",
        )
    )
    add(
        Case(
            "payouts.preview.missing_required",
            "POST",
            "/payouts/preview",
            body={"period_start": JUL[0]},
            expect=(422,),
        )
    )
    add(
        Case(
            "payouts.preview.malformed",
            "POST",
            "/payouts/preview",
            body="<<<not json>>>",
            expect=(422,),
            tags=("raw",),
        )
    )
    add(
        Case(
            "payouts.preview.per_day",
            "POST",
            "/payouts/preview",
            body={
                "deployment_id": f.deployment_per_day,
                "period_start": JUL[0],
                "period_end": JUL[1],
            },
            note="the only per_day engagement on file (§14 Q1)",
        )
    )
    add(Case("payouts.validate", "POST", "/payouts/validate", body=preview_65k))
    add(
        Case(
            "payouts.validate.warned",
            "POST",
            "/payouts/validate",
            body={
                "deployment_id": f.deployment_incomplete or f.deployment_65k,
                "period_start": JUL[0],
                "period_end": JUL[1],
            },
            note="bCAP incomplete attendance is a warning, not a block (§5)",
        )
    )
    add(
        Case(
            "payouts.validate.trainer_refused",
            "POST",
            "/payouts/validate",
            body=preview_65k,
            actor="lde_executive",
            expect=(403,),
        )
    )
    add(
        Case(
            "payouts.sheet.remuneration",
            "POST",
            "/payouts/remuneration-sheet.xlsx",
            body=preview_65k,
            tags=("xlsx",),
        )
    )
    add(
        Case(
            "payouts.sheet.invoice",
            "POST",
            "/payouts/invoice-sheet.xlsx",
            body=preview_65k,
            tags=("xlsx",),
        )
    )
    add(
        Case(
            "payouts.sheet.lde",
            "POST",
            "/payouts/remuneration-sheet.xlsx",
            body=preview_65k,
            actor="lde_executive",
            expect=(403,),
        )
    )
    # Replay, not creation: this period is already committed, so the handler
    # returns 200 created=false and writes nothing. See the module docstring.
    add(
        Case(
            "payouts.commit.replay",
            "POST",
            "/payouts/commit",
            body=preview_65k,
            expect=(200, 201, 409),
            tags=("write",),
            note="idempotent replay of an already-committed period",
        )
    )
    add(
        Case(
            "payouts.commit.lde",
            "POST",
            "/payouts/commit",
            body=preview_65k,
            actor="lde_executive",
            expect=(403,),
        )
    )

    # --- programs -------------------------------------------------------------
    add(
        Case(
            "programs.tasks.generate",
            "POST",
            f"/programs/{f.program_id}/tasks:generate",
            expect=(200,),
            tags=("write", "idempotent"),
        )
    )
    add(
        Case(
            "programs.documents.generate",
            "POST",
            f"/programs/{f.program_id}/documents:generate",
            expect=(200,),
            tags=("write", "idempotent"),
        )
    )
    add(
        Case(
            "programs.tasks.generate.lde",
            "POST",
            f"/programs/{f.program_id}/tasks:generate",
            actor="lde_executive",
            expect=(200, 403),
            tags=("write", "idempotent"),
            note="LDE Executive may seed their own campus checklist",
        )
    )
    add(
        Case(
            "programs.tasks.generate.missing",
            "POST",
            f"/programs/{f.missing_uuid}/tasks:generate",
            expect=(404,),
        )
    )
    add(
        Case(
            "programs.tasks.generate.out_of_reach",
            "POST",
            f"/programs/{f.program_id_out_of_reach}/tasks:generate",
            expect=(403, 404),
        )
    )
    add(
        Case(
            "programs.tasks.generate.badid",
            "POST",
            "/programs/not-a-uuid/tasks:generate",
            expect=(422,),
        )
    )

    # --- reports --------------------------------------------------------------
    add(
        Case(
            "reports.governance",
            "POST",
            f"/reports/programs/{f.program_id}/governance",
            body={"period_start": JUL[0], "period_end": JUL[1]},
        )
    )
    add(
        Case(
            "reports.governance.commercial",
            "POST",
            f"/reports/programs/{f.program_id}/governance",
            body={"period_start": JUL[0], "period_end": JUL[1], "include_trainer_cost": True},
        )
    )
    add(
        Case(
            "reports.governance.lde",
            "POST",
            f"/reports/programs/{f.program_id}/governance",
            body={"period_start": JUL[0], "period_end": JUL[1], "include_trainer_cost": True},
            actor="lde_executive",
            expect=(200, 403),
            note="commercial section must be withheld, not 500",
        )
    )
    add(
        Case(
            "reports.governance.narrative",
            "POST",
            f"/reports/programs/{f.program_id}/governance",
            body={"period_start": JUL[0], "period_end": JUL[1], "include_narrative": True},
            expect=(200, 503),
            tags=("llm",),
        )
    )
    add(
        Case(
            "reports.governance.badperiod",
            "POST",
            f"/reports/programs/{f.program_id}/governance",
            body={"period_start": "2026-12-31", "period_end": "2026-01-01"},
            expect=(422,),
        )
    )
    add(
        Case(
            "reports.feedback",
            "GET",
            f"/reports/programs/{f.program_id}/feedback"
            f"?period_start={JUL[0]}&period_end={JUL[1]}",
        )
    )
    add(
        Case(
            "reports.feedback.noparams",
            "GET",
            f"/reports/programs/{f.program_id}/feedback",
            expect=(422,),
        )
    )
    add(
        Case(
            "reports.college.summary",
            "GET",
            f"/reports/colleges/{f.college_id}/summary"
            f"?period_start={JUL[0]}&period_end={JUL[1]}",
        )
    )
    add(
        Case(
            "reports.college.summary.out_of_reach",
            "GET",
            f"/reports/colleges/{f.missing_uuid}/summary"
            f"?period_start={JUL[0]}&period_end={JUL[1]}",
            expect=(403, 404),
        )
    )

    # --- approvals ------------------------------------------------------------
    add(
        Case(
            "approvals.versions",
            "GET",
            f"/approvals/{f.artifact_type}/{f.artifact_id}/versions",
            actor=sm,
        )
    )
    add(
        Case(
            "approvals.versions.badtype",
            "GET",
            f"/approvals/not_a_type/{f.artifact_id}/versions",
            expect=(422,),
        )
    )
    add(
        Case(
            "approvals.versions.missing",
            "GET",
            f"/approvals/{f.artifact_type}/{f.missing_uuid}/versions",
            actor=sm,
            expect=(404,),
        )
    )
    add(
        Case(
            "approvals.submit.governance_501",
            "POST",
            f"/approvals/governance_reports/{f.missing_uuid}/submit",
            actor=sm,
            expect=(404, 501),
            note="§14 Q3 — authority undefined for governance_reports",
        )
    )
    add(
        Case(
            "approvals.approve.on_released",
            "POST",
            f"/approvals/{f.artifact_type}/{f.artifact_id}/approve",
            actor=sm,
            expect=(409, 501),
            tags=("write",),
            note="artifact is RELEASED; approve is not a legal transition",
        )
    )
    add(
        Case(
            "approvals.reject.noreason",
            "POST",
            f"/approvals/{f.artifact_type}/{f.artifact_id}/reject",
            actor=sm,
            body={},
            expect=(422,),
        )
    )
    add(
        Case(
            "approvals.release.on_released",
            "POST",
            f"/approvals/{f.artifact_type}/{f.artifact_id}/release",
            actor=sm,
            body={"notes": "bench probe"},
            expect=(409, 501),
            tags=("write",),
        )
    )
    add(
        Case(
            "approvals.submit.lde",
            "POST",
            f"/approvals/{f.artifact_type}/{f.artifact_id}/submit",
            actor="lde_executive",
            expect=(403, 409),
            tags=("write",),
        )
    )

    # --- comms ----------------------------------------------------------------
    add(Case("comms.list", "GET", f"/comms/messages?program_id={f.program_id}"))
    add(Case("comms.list.noprogram", "GET", "/comms/messages", expect=(422,)))
    add(
        Case(
            "comms.list.hugelimit",
            "GET",
            f"/comms/messages?program_id={f.program_id}&limit=1000000",
            expect=(200, 422),
        )
    )
    add(
        Case(
            "comms.list.negativelimit",
            "GET",
            f"/comms/messages?program_id={f.program_id}&limit=-5",
            expect=(422,),
        )
    )
    add(
        Case(
            "comms.read.missing",
            "GET",
            f"/comms/messages/{f.missing_uuid}",
            expect=(404,),
        )
    )
    add(
        Case(
            "comms.approve.501",
            "POST",
            f"/comms/messages/{f.missing_uuid}/approve",
            actor=sm,
            expect=(404, 501),
            note="§14 Q3 — COMMS_APPROVAL_AUTHORITY is deliberately empty",
        )
    )
    add(
        Case(
            "comms.reject.501",
            "POST",
            f"/comms/messages/{f.missing_uuid}/reject",
            actor=sm,
            body={"reason": "bench probe"},
            expect=(404, 501),
        )
    )
    add(
        Case(
            "comms.release.501",
            "POST",
            f"/comms/messages/{f.missing_uuid}/release",
            actor=sm,
            body={"notes": "bench probe"},
            expect=(404, 501),
        )
    )
    add(
        Case(
            "comms.draft",
            "POST",
            "/comms/messages",
            body={
                "program_id": f.program_id,
                "channel": "email",
                "recipient_kind": "internal_staff",
                "recipient_ref": "bench@example.invalid",
                "template_key": "bench.probe",
                "template": "Hello {name}, this is a benchmark probe.",
                "template_values": {"name": "Bench"},
                "subject": "bench probe",
                "body": "Hello Bench, this is a benchmark probe.",
            },
            expect=(201,),
            tags=("write", "creates:comms_messages"),
        )
    )
    add(
        Case(
            "comms.draft.badchannel",
            "POST",
            "/comms/messages",
            body={
                "program_id": f.program_id,
                "channel": "carrier_pigeon",
                "recipient_kind": "internal_staff",
                "recipient_ref": "bench@example.invalid",
                "template_key": "bench.probe",
                "template": "x",
                "body": "x",
            },
            expect=(422,),
        )
    )
    add(
        Case(
            "comms.submit.missing",
            "POST",
            f"/comms/messages/{f.missing_uuid}/submit",
            expect=(404,),
        )
    )
    add(
        Case(
            "comms.amend.missing",
            "PATCH",
            f"/comms/messages/{f.missing_uuid}",
            body={"body": "amended"},
            expect=(404,),
        )
    )
    add(
        Case(
            "comms.supersede.missing",
            "POST",
            f"/comms/messages/{f.missing_uuid}/supersede",
            body={"body": "replacement"},
            expect=(404,),
        )
    )

    # --- erm ------------------------------------------------------------------
    add(Case("erm.list", "GET", "/erm/tasks"))
    add(Case("erm.list.filtered", "GET", "/erm/tasks?state=queued&subject_kind=trainer"))
    add(Case("erm.list.badstate", "GET", "/erm/tasks?state=nonsense", expect=(422,)))
    add(Case("erm.list.hugelimit", "GET", "/erm/tasks?limit=1000000", expect=(200, 422)))
    add(Case("erm.read.missing", "GET", f"/erm/tasks/{f.missing_uuid}", expect=(404,)))
    add(
        Case(
            "erm.create",
            "POST",
            "/erm/tasks",
            body={"subject_kind": "trainer", "subject_id": f.trainer_id},
            expect=(201, 409),
            tags=("write", "creates:erm_sync_tasks"),
        )
    )
    add(
        Case(
            "erm.create.badkind",
            "POST",
            "/erm/tasks",
            body={"subject_kind": "spaceship", "subject_id": f.trainer_id},
            expect=(422,),
        )
    )
    add(
        Case(
            "erm.create.missing_subject",
            "POST",
            "/erm/tasks",
            body={"subject_kind": "trainer", "subject_id": f.missing_uuid},
            expect=(403, 404),
        )
    )
    add(
        Case(
            "erm.assign.missing",
            "POST",
            f"/erm/tasks/{f.missing_uuid}/assign",
            body={"assignee_id": f.assignee_id},
            expect=(404,),
        )
    )
    add(
        Case(
            "erm.confirm.missing",
            "POST",
            f"/erm/tasks/{f.missing_uuid}/confirm",
            body={"verified": True},
            expect=(404,),
        )
    )
    add(
        Case(
            "erm.cancel.missing",
            "POST",
            f"/erm/tasks/{f.missing_uuid}/cancel",
            body={"reason": "bench probe"},
            expect=(404,),
        )
    )

    # --- cross-cutting auth probes -------------------------------------------
    add(Case("auth.badtoken", "GET", "/monitoring/rules", actor="badtoken", expect=(401,)))
    add(Case("auth.algnone", "GET", "/monitoring/rules", actor="algnone", expect=(401,)))
    add(Case("auth.expired", "GET", "/monitoring/rules", actor="expired", expect=(401,)))
    add(Case("auth.noprofile", "GET", "/monitoring/rules", actor="noprofile", expect=(401,)))

    return cases


# --- transport ----------------------------------------------------------------


def _loop_factory() -> asyncio.AbstractEventLoop:
    """The selector loop psycopg needs. Same choice `run_api.py` makes."""
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


@contextmanager
def _client(base_url: str | None) -> Iterator[Any]:
    """A TestClient over the app in-process, or an httpx client against a server."""
    if base_url:
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=120.0) as client:
            yield client
        return

    from fastapi.testclient import TestClient  # imported late: pulls in the app

    from app.main import create_app

    with TestClient(
        create_app(),
        backend_options={"loop_factory": _loop_factory},
        raise_server_exceptions=False,  # a 500 is a finding to report, not to raise
    ) as client:
        yield client


def _special_token(label: str, actors: dict[str, Actor]) -> str | None:
    """Deliberately bad credentials, for the auth probes."""
    secret = os.environ.get("SUPABASE_JWT_SECRET", "")
    now = int(time.time())
    if label == "badtoken":
        return "not.a.jwt"
    if label == "algnone":
        # An unsigned token claiming alg:none — the classic confusion attack.
        import base64

        def seg(d: dict[str, Any]) -> str:
            return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

        return f"{seg({'alg': 'none', 'typ': 'JWT'})}.{seg({'sub': str(uuid.uuid4())})}."
    if label == "expired":
        return jwt.encode(
            {"sub": str(uuid.uuid4()), "aud": "authenticated", "exp": now - 3600},
            secret,
            algorithm="HS256",
        )
    if label == "noprofile":
        return jwt.encode(
            {"sub": str(uuid.uuid4()), "aud": "authenticated", "exp": now + 3600},
            secret,
            algorithm="HS256",
        )
    return None


def _headers(case: Case, actors: dict[str, Actor]) -> dict[str, str]:
    special = _special_token(case.actor, actors)
    if special is not None:
        return {"Authorization": f"Bearer {special}"}
    actor = actors.get(case.actor)
    if actor is None:
        return {}
    tok = token_for(actor)
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _send(
    client: Any, case: Case, actors: dict[str, Actor]
) -> tuple[int, float, Any, dict[str, str]]:
    headers = _headers(case, actors)
    kwargs: dict[str, Any] = {"headers": headers}
    if case.body is not None:
        if "raw" in case.tags:
            kwargs["content"] = case.body
            kwargs["headers"] = {**headers, "Content-Type": "application/json"}
        else:
            kwargs["json"] = case.body

    started = time.perf_counter()
    response = client.request(case.method, case.path, **kwargs)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    payload: Any
    ctype = response.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = response.json()
        except Exception as exc:  # pragma: no cover - a malformed JSON body is a finding
            payload = {"__decode_error__": str(exc)}
    elif "spreadsheetml" in ctype:
        payload = {"__xlsx_bytes__": len(response.content)}
    else:
        payload = {"__text__": response.text[:400]}
    return response.status_code, elapsed_ms, payload, dict(response.headers)


# --- assertions ---------------------------------------------------------------


def _dig(payload: Any, dotted: str) -> Any:
    cur = payload
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)]
        else:
            return None
    return cur


def _check(case: Case, status: int, payload: Any) -> list[str]:
    """Everything wrong with one response. Empty means it behaved."""
    problems: list[str] = []
    if status >= 500:
        problems.append(f"HTTP {status} — server error: {json.dumps(payload)[:300]}")
    elif status not in case.expect:
        problems.append(
            f"HTTP {status}, expected {'/'.join(map(str, case.expect))}: "
            f"{json.dumps(payload)[:300]}"
        )
    if case.assert_json and status in case.expect and status < 300:
        for path, want in case.assert_json.items():
            got = _dig(payload, path)
            if str(got) != str(want):
                problems.append(f"{path}: expected {want!r}, got {got!r}")
    return problems


# --- percentiles --------------------------------------------------------------


def _pct(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # Nearest-rank. With 10 samples an interpolated p99 is a fiction; the rank is
    # at least an observation that really happened.
    rank = max(1, min(len(ordered), int(round(q * len(ordered) + 0.5))))
    return ordered[rank - 1]


@dataclass
class Result:
    case: Case
    status: int
    cold_ms: float
    warm: list[float] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def p50(self) -> float:
        return _pct(self.warm, 0.50)

    @property
    def p95(self) -> float:
        return _pct(self.warm, 0.95)

    @property
    def p99(self) -> float:
        return _pct(self.warm, 0.99)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.warm) if self.warm else 0.0


# --- cleanup ------------------------------------------------------------------


@dataclass
class _Created:
    """Primary keys this run inserted, so the run can take them back out.

    Only ever populated from an id the API itself returned for a row this process
    caused to exist. Nothing else is ever deleted.
    """

    comms_messages: list[str] = field(default_factory=list)
    erm_sync_tasks: list[str] = field(default_factory=list)
    tasks_before: set[str] = field(default_factory=set)
    program_documents_before: set[str] = field(default_factory=set)

    def snapshot(self, dsn: str, program_id: str) -> None:
        if not program_id:
            return
        with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
            cur.execute("select id from tasks where program_id = %s", (program_id,))
            self.tasks_before = {str(r[0]) for r in cur.fetchall()}
            cur.execute("select id from program_documents where program_id = %s", (program_id,))
            self.program_documents_before = {str(r[0]) for r in cur.fetchall()}

    def cleanup(self, dsn: str, program_id: str) -> list[str]:
        removed: list[str] = []
        with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
            for table, ids in (
                ("comms_messages", self.comms_messages),
                ("erm_sync_tasks", self.erm_sync_tasks),
            ):
                for row_id in ids:
                    cur.execute(f"delete from {table} where id = %s", (row_id,))
                    removed.append(f"{table}:{row_id}")
            if program_id:
                cur.execute("select id from tasks where program_id = %s", (program_id,))
                for (row_id,) in cur.fetchall():
                    if str(row_id) not in self.tasks_before:
                        cur.execute("delete from tasks where id = %s", (row_id,))
                        removed.append(f"tasks:{row_id}")
                cur.execute("select id from program_documents where program_id = %s", (program_id,))
                for (row_id,) in cur.fetchall():
                    if str(row_id) not in self.program_documents_before:
                        cur.execute("delete from program_documents where id = %s", (row_id,))
                        removed.append(f"program_documents:{row_id}")
            conn.commit()
        return removed


# --- the run ------------------------------------------------------------------


def run(
    cases: Sequence[Case],
    actors: dict[str, Actor],
    *,
    base_url: str | None,
    iterations: int,
    created: _Created,
) -> list[Result]:
    results: list[Result] = []
    with _client(base_url) as client:
        for case in cases:
            status, cold, payload, headers = _send(client, case, actors)
            result = Result(
                case=case, status=status, cold_ms=cold, payload=payload, headers=headers
            )

            # Remember anything we just created so `cleanup` can take it back out.
            if isinstance(payload, dict) and status in (200, 201):
                if case.path == "/comms/messages" and case.method == "POST" and payload.get("id"):
                    created.comms_messages.append(str(payload["id"]))
                if case.path == "/erm/tasks" and case.method == "POST" and payload.get("id"):
                    created.erm_sync_tasks.append(str(payload["id"]))

            repeats = 1 if ("llm" in case.tags or "write" in case.tags) else iterations
            for _ in range(repeats):
                s2, ms, _, _ = _send(client, case, actors)
                result.warm.append(ms)
                if s2 != status:
                    result.problems.append(f"non-deterministic status: {status} then {s2}")
                    status = s2
            result.problems.extend(_check(case, result.status, payload))
            results.append(result)
            flag = "FAIL" if result.problems else "ok  "
            print(
                f"  [{flag}] {case.name:42} {case.method:6} {case.path[:48]:48} "
                f"{result.status}  p50={result.p50:7.1f}ms",
                flush=True,
            )
    return results


# --- lifecycle sweeps ---------------------------------------------------------
#
# Some routes cannot be reached by a table of independent requests: approving a
# comms message needs a message that this run drafted and submitted, and the
# deliberate 501s (§14 Q3) sit BEHIND the row lookup, so probing them with a
# random UUID reports 404 and proves nothing. These flows create the row, walk it
# through every route that acts on it, and delete it again.


def lifecycle(
    client: Any, actors: dict[str, Actor], f: Fixtures, dsn: str
) -> list[tuple[str, str, str, int, str]]:
    """Walk the chained routes. Returns (flow, method, path, status, detail)."""
    out: list[tuple[str, str, str, int, str]] = []
    created_comms: list[str] = []
    created_versions: list[str] = []
    created_erm: list[str] = []
    created_docs: list[str] = []
    created_tasks: list[str] = []

    def call(flow: str, method: str, path: str, actor: str, body: Any = None) -> Any:
        headers = _headers(Case("", method, path, actor=actor), actors)
        kw: dict[str, Any] = {"headers": headers}
        if body is not None:
            kw["json"] = body
        r = client.request(method, path, **kw)
        try:
            payload = r.json()
        except Exception:
            payload = {"__text__": r.text[:200]}
        out.append((flow, method, path.split("?")[0], r.status_code, json.dumps(payload)[:220]))
        print(f"  [{flow}] {method:6} {path.split('?')[0][:52]:52} -> {r.status_code}", flush=True)
        return payload if r.status_code < 400 else None

    try:
        # --- comms: draft -> read -> amend -> submit -> approve/reject/release ---
        drafted = call(
            "comms",
            "POST",
            "/comms/messages",
            "manager",
            {
                "program_id": f.program_id,
                "channel": "email",
                "recipient_kind": "internal_staff",
                "recipient_ref": "bench@example.invalid",
                "template_key": "bench.lifecycle",
                "template": "Hello {name}, this is a benchmark probe.",
                "template_values": {"name": "Bench"},
                "subject": "bench lifecycle",
                "body": "Hello Bench, this is a benchmark probe. Edited line.",
            },
        )
        if drafted and drafted.get("id"):
            mid = drafted["id"]
            created_comms.append(mid)
            call("comms", "GET", f"/comms/messages/{mid}", "manager")
            call("comms", "PATCH", f"/comms/messages/{mid}", "manager", {"body": "Amended body."})
            call("comms", "POST", f"/comms/messages/{mid}/submit", "manager")
            # The three §14 Q3 endpoints. 501 is the correct answer today.
            call("comms", "POST", f"/comms/messages/{mid}/approve", "senior_manager")
            call(
                "comms",
                "POST",
                f"/comms/messages/{mid}/reject",
                "senior_manager",
                {"reason": "bench probe"},
            )
            call(
                "comms",
                "POST",
                f"/comms/messages/{mid}/release",
                "senior_manager",
                {"notes": "bench probe"},
            )
            superseded = call(
                "comms",
                "POST",
                f"/comms/messages/{mid}/supersede",
                "manager",
                {"body": "Replacement body."},
            )
            if superseded and superseded.get("id"):
                created_comms.append(superseded["id"])

        # --- erm: create -> read -> assign -> confirm -> cancel ------------------
        task = call(
            "erm",
            "POST",
            "/erm/tasks",
            "manager",
            {"subject_kind": "trainer", "subject_id": f.trainer_id},
        )
        if task and task.get("id"):
            tid = task["id"]
            created_erm.append(tid)
            call("erm", "GET", f"/erm/tasks/{tid}", "manager")
            call(
                "erm", "POST", f"/erm/tasks/{tid}/assign", "manager", {"assignee_id": f.assignee_id}
            )
            call(
                "erm",
                "POST",
                f"/erm/tasks/{tid}/confirm",
                "manager",
                {"erm_external_id": "BENCH-PROBE", "verified": True, "remarks": "bench"},
            )
            call("erm", "POST", f"/erm/tasks/{tid}/cancel", "manager", {"reason": "bench probe"})

        # --- programs: generation, idempotent --------------------------------
        docs_before = _doc_ids(dsn, f.program_id)
        tasks_before = _task_ids(dsn, f.program_id)
        call("programs", "POST", f"/programs/{f.program_id}/tasks:generate", "manager")
        call("programs", "POST", f"/programs/{f.program_id}/tasks:generate", "manager")
        call("programs", "POST", f"/programs/{f.program_id}/documents:generate", "manager")
        call("programs", "POST", f"/programs/{f.program_id}/documents:generate", "lde_executive")
        created_docs = sorted(_doc_ids(dsn, f.program_id) - docs_before)
        created_tasks = sorted(_task_ids(dsn, f.program_id) - tasks_before)

        # --- approvals: submit an artifact type with NO authority (§14 Q3) -------
        # `program_documents` has no APPROVAL_AUTHORITY entry, so approve and
        # reject must answer 501. Run it against a document THIS flow generated,
        # inside the caller's reach, so the 501 is actually reached rather than
        # masked by a 403 or a 404 — and so the row can be deleted afterwards.
        doc_id = created_docs[0] if created_docs else ""
        if doc_id:
            before = _version_ids(dsn, doc_id)
            call("approvals", "POST", f"/approvals/program_documents/{doc_id}/submit", "manager")
            call("approvals", "GET", f"/approvals/program_documents/{doc_id}/versions", "manager")
            call(
                "approvals",
                "POST",
                f"/approvals/program_documents/{doc_id}/approve",
                "senior_manager",
            )
            call(
                "approvals",
                "POST",
                f"/approvals/program_documents/{doc_id}/reject",
                "senior_manager",
                {"reason": "bench probe"},
            )
            call(
                "approvals",
                "POST",
                f"/approvals/program_documents/{doc_id}/release",
                "senior_manager",
                {"notes": "bench probe"},
            )
            created_versions = sorted(_version_ids(dsn, doc_id) - before)

        # --- payouts: commit replay (writes nothing) -------------------------
        call(
            "payouts",
            "POST",
            "/payouts/commit",
            "manager",
            {
                "deployment_id": f.deployment_65k,
                "period_start": JUL[0],
                "period_end": JUL[1],
            },
        )
    finally:
        removed = []
        with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
            # Children first: a superseding message references its predecessor.
            for mid in reversed(created_comms):
                cur.execute("delete from comms_messages where id = %s", (mid,))
                removed.append(f"comms_messages:{mid}")
            for tid in created_erm:
                cur.execute("delete from erm_sync_tasks where id = %s", (tid,))
                removed.append(f"erm_sync_tasks:{tid}")
            # Versions before the documents they point at — the FK runs that way.
            for vid in created_versions:
                cur.execute("delete from artifact_versions where id = %s", (vid,))
                removed.append(f"artifact_versions:{vid}")
            for did in created_docs:
                cur.execute("delete from program_documents where id = %s", (did,))
                removed.append(f"program_documents:{did}")
            for tid in created_tasks:
                cur.execute("delete from tasks where id = %s", (tid,))
                removed.append(f"tasks:{tid}")
            conn.commit()
        print(f"\n  lifecycle cleanup: removed {len(removed)} row(s) it created")
        for r in removed:
            print(f"    {r}")
    return out


def _version_ids(dsn: str, artifact_id: str) -> set[str]:
    with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("select id from artifact_versions where artifact_id = %s", (artifact_id,))
        return {str(r[0]) for r in cur.fetchall()}


def _doc_ids(dsn: str, program_id: str) -> set[str]:
    with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("select id from program_documents where program_id = %s", (program_id,))
        return {str(r[0]) for r in cur.fetchall()}


def _task_ids(dsn: str, program_id: str) -> set[str]:
    with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("select id from tasks where program_id = %s", (program_id,))
        return {str(r[0]) for r in cur.fetchall()}


# --- reporting ----------------------------------------------------------------


def markdown(results: Sequence[Result], *, iterations: int) -> str:
    db = [r for r in results if "llm" not in r.case.tags]
    llm = [r for r in results if "llm" in r.case.tags]
    out: list[str] = []

    def table(rows: Sequence[Result], title: str) -> None:
        out.append(f"\n### {title}\n")
        out.append(
            "| Case | Method | Path | Actor | Status | cold ms | p50 | p95 | p99 | Verdict |"
        )
        out.append("|---|---|---|---|---:|---:|---:|---:|---:|---|")
        for r in sorted(rows, key=lambda x: -x.p95):
            verdict = "PASS" if not r.problems else "**FAIL** " + "; ".join(r.problems)[:160]
            out.append(
                f"| `{r.case.name}` | {r.case.method} | `{r.case.path.split('?')[0]}` | "
                f"{r.case.actor} | {r.status} | {r.cold_ms:.0f} | {r.p50:.0f} | "
                f"{r.p95:.0f} | {r.p99:.0f} | {verdict} |"
            )

    out.append(f"_{len(results)} cases, {iterations} warm iterations each._")
    table(db, "Database-bound and pure routes")
    if llm:
        table(llm, "LLM-backed routes (OpenRouter — network-bound, not comparable)")
    fails = [r for r in results if r.problems]
    out.append(f"\n**{len(fails)} of {len(results)} cases failed.**\n")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--iterations", type=int, default=10, help="warm calls per case")
    parser.add_argument("--base-url", default=None, help="benchmark a running server instead")
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--include-llm", action="store_true")
    parser.add_argument("--json", dest="json_out", default=None)
    parser.add_argument("--markdown", dest="md_out", default=None)
    parser.add_argument("--only", default=None, help="substring filter on case name")
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="walk the chained flows (comms, erm, approvals, programs) and clean up after",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set.")

    print("Resolving actors and fixtures from the database…")
    actors = discover_actors(dsn)
    fixtures = discover_fixtures(dsn, actors)
    prefer_reaching_actors(dsn, actors, fixtures.college_id)
    for label, actor in actors.items():
        print(f"  {label:26} {actor.note}")
    print(f"  fixtures: {fixtures}")

    cases = build_cases(fixtures, actors)
    if not args.allow_writes:
        cases = [c for c in cases if "write" not in c.tags]
    if not args.include_llm:
        cases = [c for c in cases if "llm" not in c.tags]
    if args.only:
        cases = [c for c in cases if args.only in c.name]
    # A case whose actor was not discovered would benchmark an anonymous call
    # under a persona's name, which is worse than skipping it.
    known = set(actors) | {"badtoken", "algnone", "expired", "noprofile"}
    skipped = [c.name for c in cases if c.actor not in known]
    cases = [c for c in cases if c.actor in known]

    if args.lifecycle:
        print("\nLifecycle sweep — creates rows, walks every chained route, deletes them.\n")
        with _client(args.base_url) as client:
            rows = lifecycle(client, actors, fixtures, dsn)
        print(
            f"\n{len(rows)} lifecycle calls; statuses " f"{sorted({s for _, _, _, s, _ in rows})}"
        )
        # 501 and 503 are deliberate answers here, not crashes: 501 is the §14 Q3
        # authority gap and 503 is "the Copilot is not configured on this
        # deployment". An unhandled exception is 500, and only that is a failure.
        bad = [r for r in rows if r[3] >= 500 and r[3] not in (501, 503)]
        for flow, method, path, status_code, detail in rows:
            print(f"  {flow:10} {method:6} {path[:56]:56} {status_code}  {detail[:110]}")
        print(f"\n{len(bad)} lifecycle call(s) returned 5xx")
        return 1 if bad else 0

    created = _Created()
    if args.allow_writes:
        created.snapshot(dsn, fixtures.program_id)

    print(f"\nRunning {len(cases)} cases × {args.iterations} warm iterations…\n")
    started = time.time()
    try:
        results = run(
            cases, actors, base_url=args.base_url, iterations=args.iterations, created=created
        )
    finally:
        if args.allow_writes:
            removed = created.cleanup(dsn, fixtures.program_id)
            print(f"\nCleaned up {len(removed)} row(s) this run created: {removed}")

    elapsed = time.time() - started
    fails = [r for r in results if r.problems]
    print(f"\n{len(results)} cases in {elapsed:.1f}s — {len(fails)} failing")
    if skipped:
        print(f"skipped (actor not present in this database): {skipped}")
    for r in fails:
        print(f"\nFAIL {r.case.name}  {r.case.method} {r.case.path}  [{r.case.actor}]")
        for p in r.problems:
            print(f"     {p}")

    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(markdown(results, iterations=args.iterations))
        print(f"\nwrote {args.md_out}")
    if args.json_out:
        payload = [
            {
                "name": r.case.name,
                "method": r.case.method,
                "path": r.case.path,
                "actor": r.case.actor,
                "tags": list(r.case.tags),
                "note": r.case.note,
                "status": r.status,
                "expected": list(r.case.expect),
                "cold_ms": round(r.cold_ms, 2),
                "p50_ms": round(r.p50, 2),
                "p95_ms": round(r.p95, 2),
                "p99_ms": round(r.p99, 2),
                "mean_ms": round(r.mean, 2),
                "samples": len(r.warm),
                "problems": r.problems,
                "body": r.payload,
            }
            for r in results
        ]
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(
                {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "results": payload},
                fh,
                indent=1,
                default=str,
            )
        print(f"wrote {args.json_out}")

    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
