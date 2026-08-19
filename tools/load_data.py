"""Generate and flood-load a realistic large-scale dataset, then remove it exactly.

    python tools/load_data.py --plan                # planned counts, no I/O
    python tools/load_data.py --status
    python tools/load_data.py --create --scale 10
    python tools/load_data.py --counts
    python tools/load_data.py --delete

WHY THIS IS SEPARATE FROM tools/demo_data.py
============================================
`demo_data.py` seeds a *walkthrough*: five educators, one college, numbers a
human reconciles by hand against CLAUDE.md §6. This seeds a *load test*: enough
colleges, programs, trainers and trainer-days to make an index choice or an RLS
predicate visible in a latency number. The two must be removable independently,
so this file carries its OWN namespace (`LOAD_NAMESPACE`) and never addresses a
row `demo_data.py` created — or vice versa.

HOW DELETION IS MADE EXACT
--------------------------
Identical discipline to `demo_data.py`, for identical reasons. Every row gets
`uuid5(LOAD_NAMESPACE, label)`, so:

* **Idempotent.** Re-running `--create` writes the same ids and upserts.
* **Supersetting.** A label depends on an entity's INDEX, never on the total
  count, so scale 10 is a strict superset of scale 1. Ramping 1x -> 10x -> 100x
  is additive: the rows measured at the small rung are the same rows at the
  large one, which is what makes the latency curve a curve and not three
  unrelated readings.
* **Exact.** `--delete` removes by primary key, or by a foreign key whose values
  are themselves ids from this namespace. There is no `LIKE '%load%'`, no date
  sweep, no `truncate`, and no statement anywhere in this file that can address
  a row it did not create. `--delete` defaults to the id space of the LARGEST
  scale, so it cleans up regardless of which rung was last run.

THE SHAPE IS SKEWED ON PURPOSE
------------------------------
Uniform noise does not break anything interesting. Real ops data is Zipf-ish:
a handful of enormous campuses carry most of the trainers, and a long tail of
small ones carry two programs each. `size_class()` reproduces that, so a Senior
Manager over the cluster containing college 0 has genuinely more reach to
resolve than one over a cluster of small colleges — which is precisely the
variable that decides whether `can_reach_college()` is cheap.

CLAUDE.md §5 is honoured: both program types exist, CRT carries per-day rates
and bCAP per-month, and the attendance marks differ accordingly. CLAUDE.md §6:
attendance is ONE ROW PER DAY. R7: every rupee in here is a `Decimal`.

WHAT IT REFUSES TO TOUCH
------------------------
Anything outside the id set it computes. The only DDL it issues is
`create temp table ... on commit drop`, which is session-local, vanishes on
disconnect, and exists solely so bulk rows can arrive by `COPY` and then land
through `insert ... on conflict do nothing`. No schema, index or policy in
`supabase/migrations/` is read, written or altered.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("psycopg is not installed. Run inside the project venv.")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: Fixed, and DISTINCT from demo_data.py's DEMO_NAMESPACE. Every load-test id
#: derives from it; regenerating it orphans existing rows and breaks --delete.
LOAD_NAMESPACE: Final[uuid.UUID] = uuid.UUID("10ad7e57-0000-4000-8000-000000000001")

LOAD_EMAIL_DOMAIN: Final[str] = "loadtest.bytexl.in"
LOAD_PASSWORD: Final[str] = "LoadTest!2026"

#: The observation window. Deployments start inside it and attendance is marked
#: day by day across each deployment's own window, clipped to the end. A bounded
#: window means the HomePage month query (`mark_date between`) has a realistic
#: selectivity instead of matching either everything or nothing.
WINDOW_START: Final[dt.date] = dt.date(2026, 2, 1)
WINDOW_END: Final[dt.date] = dt.date(2026, 8, 31)
WO_FROM: Final[dt.date] = dt.date(2026, 4, 1)
WO_TO: Final[dt.date] = dt.date(2027, 3, 31)

SCALES: Final[tuple[int, ...]] = (1, 10, 100)
MAX_SCALE: Final[int] = max(SCALES)
COLLEGES_PER_SCALE: Final[int] = 5
COLLEGES_PER_CLUSTER: Final[int] = 12


def load_id(label: str) -> uuid.UUID:
    """The id for `label`. Stable across machines, runs and scales."""
    return uuid.uuid5(LOAD_NAMESPACE, label)


def rng_for(label: str) -> random.Random:
    """A generator seeded from the label alone.

    Deliberately NOT a shared stream. A shared `random.Random(0)` consumed in
    creation order makes every attribute depend on how many entities came
    before, so scale 10 would rewrite the rows scale 1 wrote and the ramp would
    stop being a ramp.
    """
    return random.Random(load_id(label).int & 0xFFFFFFFF)


# --- the shape ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SizeClass:
    name: str
    programs: int
    batches_per_program: int
    trainers: int
    deployments_per_batch: int


#: A few very large colleges, many small ones. 4% mega, 16% large, 40% medium,
#: 40% small — the standard ops long tail. CLAUDE.md gives no number for this
#: (§14 Q7 is still open on peak concurrency), so the ratio is stated here
#: rather than hidden in a literal.
SIZES: Final[dict[str, SizeClass]] = {
    "mega": SizeClass("mega", 15, 3, 90, 2),
    "large": SizeClass("large", 8, 2, 40, 2),
    "medium": SizeClass("medium", 4, 2, 18, 2),
    "small": SizeClass("small", 2, 2, 8, 1),
}


def size_class(college_index: int) -> SizeClass:
    """Which size band college `i` belongs to. A pure function of the index.

    Index-only, so a college keeps its size as the scale grows. If this consulted
    the total count the 1x colleges would change shape at 10x and the ramp would
    compare different populations.
    """
    if college_index % 25 == 0:
        return SIZES["mega"]
    if college_index % 5 == 0:
        return SIZES["large"]
    if college_index % 5 in (1, 2):
        return SIZES["medium"]
    return SIZES["small"]


CITIES: Final[tuple[str, ...]] = (
    "Hyderabad",
    "Vijayawada",
    "Guntur",
    "Visakhapatnam",
    "Warangal",
    "Tirupati",
    "Rajahmundry",
    "Nellore",
    "Kakinada",
    "Karimnagar",
    "Bengaluru",
    "Chennai",
    "Coimbatore",
    "Pune",
    "Nagpur",
    "Bhubaneswar",
)
BANKS: Final[tuple[tuple[str, str], ...]] = (
    ("State Bank of India", "SBIN"),
    ("HDFC Bank", "HDFC"),
    ("ICICI Bank", "ICIC"),
    ("Axis Bank", "UTIB"),
    ("Kotak Mahindra Bank", "KKBK"),
    ("Union Bank of India", "UBIN"),
    ("Canara Bank", "CNRB"),
    ("Punjab National Bank", "PUNB"),
)
BRANCHES: Final[tuple[str, ...]] = ("CSE", "IT", "ECE", "EEE", "MECH", "CIVIL", "AIML", "DS")
STAGES: Final[tuple[str, ...]] = (
    "acquisition_setup",
    "trainer_sourcing",
    "trainer_onboarding",
    "deployment",
    "active_monitoring",
    "closeout_finance",
)
TASK_STATUSES: Final[tuple[str, ...]] = ("pending", "in_progress", "done", "blocked")

#: R7. Rates are Decimal from the moment they exist. bCAP is per_month, CRT is
#: per_day (CLAUDE.md §5) — the branch is carried all the way into the data, so
#: a payout benchmark exercises both arms of §6's formula.
BCAP_RATES: Final[tuple[Decimal, ...]] = tuple(
    Decimal(v) for v in ("45000", "50000", "55000", "60000", "65000", "70000", "80000", "95000")
)
CRT_RATES: Final[tuple[Decimal, ...]] = tuple(
    Decimal(v) for v in ("2500", "3000", "3500", "4000", "4500", "5000")
)

_LETTERS: Final[str] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def load_pan(index: int) -> str:
    """A structurally valid, deliberately fictitious PAN: 5 letters, 4 digits, 1 letter.

    CLAUDE.md §6 makes PAN the trainer identity and the seed of the invoice
    number, and `trainers_pan_key` is UNIQUE, so this has to be injective. The
    `LT` prefix keeps the whole namespace visually distinct from a real PAN and
    from demo_data.py's `DEM` prefix.
    """
    a, b, c = index // 676 % 26, index // 26 % 26, index % 26
    return (
        f"LT{_LETTERS[a]}{_LETTERS[b]}{_LETTERS[c]}{index % 10000:04d}{_LETTERS[(index * 7) % 26]}"
    )


# --- personas -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadUser:
    label: str
    email: str
    full_name: str
    role: str

    @property
    def id(self) -> uuid.UUID:
        return load_id(self.label)


#: Three reach widths, which is the whole point. The Senior Manager resolves a
#: whole cluster through `my_college_ids()`; the Manager resolves eight direct
#: assignments; the LDE Executive resolves one. CLAUDE.md §4 says persona is not
#: reach, and a latency table that does not separate the two cannot show which
#: of the two is the cost.
LOAD_USERS: Final[tuple[LoadUser, ...]] = (
    LoadUser("user:sm", f"lt-sm@{LOAD_EMAIL_DOMAIN}", "LoadTest Senior Manager", "senior_manager"),
    LoadUser("user:mgr", f"lt-mgr@{LOAD_EMAIL_DOMAIN}", "LoadTest Manager", "manager"),
    LoadUser("user:lde", f"lt-lde@{LOAD_EMAIL_DOMAIN}", "LoadTest LDE Executive", "lde_executive"),
)

#: The Manager's eight colleges. College 0 is `mega`, so the Manager's reach
#: includes the single biggest campus — the realistic bad case, not the average.
MANAGER_COLLEGES: Final[tuple[int, ...]] = (0, 1, 2, 3, 4, 5, 6, 7)
LDE_COLLEGE: Final[int] = 0


# --- generation ---------------------------------------------------------------
# Every generator yields plain tuples in column order. Nothing is materialised
# as a list of dicts: at 100x the attendance generator emits ~700k rows and the
# whole point is that it streams into COPY rather than into memory.


@dataclass(frozen=True, slots=True)
class Plan:
    """The full id/attribute plan for one scale. Cheap to build, no I/O."""

    scale: int
    colleges: int

    def cluster_count(self) -> int:
        return max(1, -(-self.colleges // COLLEGES_PER_CLUSTER))

    def cluster_rows(self) -> list[tuple[Any, ...]]:
        return [
            (str(load_id(f"cluster:{i}")), f"LoadTest Cluster {i:03d}")
            for i in range(self.cluster_count())
        ]

    def college_rows(self) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        for i in range(self.colleges):
            r = rng_for(f"college:{i}")
            size = size_class(i)
            rows.append(
                (
                    str(load_id(f"college:{i}")),
                    str(load_id(f"cluster:{i // COLLEGES_PER_CLUSTER}")),
                    f"LoadTest {size.name.title()} College {i:04d}",
                    CITIES[i % len(CITIES)],
                    r.choice(("signed", "sent", "in_progress")),
                    r.choice(("signed", "sent", "not_started")),
                )
            )
        return rows

    def program_type(self, i: int, j: int) -> str:
        """CLAUDE.md §5's central branch, seeded into the population at ~60/40.

        A free function of (i, j) because deployments, work orders and
        attendance all need the same answer and must not re-roll it.
        """
        return "bCAP" if rng_for(f"program:{i}:{j}").random() < 0.6 else "CRT"

    def program_rows(self) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        for i in range(self.colleges):
            size = size_class(i)
            for j in range(size.programs):
                r = rng_for(f"programattrs:{i}:{j}")
                ptype = self.program_type(i, j)
                start = WINDOW_START + dt.timedelta(days=r.randrange(0, 120))
                end = start + dt.timedelta(days=r.randrange(120, 260))
                rows.append(
                    (
                        str(load_id(f"program:{i}:{j}")),
                        str(load_id(f"college:{i}")),
                        ptype,
                        f"LoadTest {ptype} 2026-27 {BRANCHES[j % len(BRANCHES)]} "
                        f"C{i:04d}P{j:02d}",
                        start,
                        end,
                        r.choice(STAGES),
                    )
                )
        return rows

    def batch_rows(self) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        for i in range(self.colleges):
            size = size_class(i)
            for j in range(size.programs):
                for k in range(size.batches_per_program):
                    r = rng_for(f"batch:{i}:{j}:{k}")
                    rows.append(
                        (
                            str(load_id(f"batch:{i}:{j}:{k}")),
                            str(load_id(f"program:{i}:{j}")),
                            f"{BRANCHES[j % len(BRANCHES)]}-{_LETTERS[k]} C{i:04d}P{j:02d}",
                            BRANCHES[j % len(BRANCHES)],
                            _LETTERS[k],
                            2027 + (k % 2),
                            r.randrange(40, 180),
                        )
                    )
        return rows

    def student_rows(self) -> Iterator[tuple[Any, ...]]:
        """Six students per batch. Enough for the HomePage count query to have a
        real row count to scan; not so many that students dominate the load."""
        for i in range(self.colleges):
            size = size_class(i)
            for j in range(size.programs):
                for k in range(size.batches_per_program):
                    for s in range(6):
                        yield (
                            str(load_id(f"student:{i}:{j}:{k}:{s}")),
                            str(load_id(f"batch:{i}:{j}:{k}")),
                            f"LoadTest Student {i:04d}{j:02d}{k}{s}",
                            f"LT{i:04d}{j:02d}{k}{s}",
                        )

    def task_rows(self) -> Iterator[tuple[Any, ...]]:
        """Twelve tasks per program. `supabase/seed.sql` ships 37 templates; a
        program in flight typically has a stage's worth open, not all 37."""
        for i in range(self.colleges):
            size = size_class(i)
            for j in range(size.programs):
                for t in range(12):
                    r = rng_for(f"task:{i}:{j}:{t}")
                    status = r.choices(TASK_STATUSES, weights=(35, 20, 35, 10))[0]
                    yield (
                        str(load_id(f"task:{i}:{j}:{t}")),
                        str(load_id(f"program:{i}:{j}")),
                        STAGES[t % len(STAGES)],
                        f"LoadTest task {t:02d} for C{i:04d}P{j:02d}",
                        status,
                        WINDOW_START + dt.timedelta(days=r.randrange(0, 200)),
                        "Waiting on HR" if status == "blocked" else None,
                    )

    def trainer_indices(self) -> Iterator[tuple[int, int, int]]:
        """(college_index, local_index, global_index) for every trainer.

        The global index seeds the PAN and must be stable across scales, so it is
        the running total over colleges in index order — which it is, because
        every college's trainer count is a function of its index alone.
        """
        running = 0
        for i in range(self.colleges):
            size = size_class(i)
            for n in range(size.trainers):
                yield i, n, running
                running += 1

    def trainer_rows(self) -> Iterator[tuple[Any, ...]]:
        for i, n, g in self.trainer_indices():
            r = rng_for(f"trainer:{i}:{n}")
            yield (
                str(load_id(f"trainer:{i}:{n}")),
                load_pan(g),
                f"LoadTest Educator {g:06d}",
                f"lt{g:06d}@{LOAD_EMAIL_DOMAIN}",
                r.choice(("freelancer", "freelancer", "freelancer", "full_timer")),
                # §7 makes "signed work order on file" a BLOCKING gate. ~80%
                # signed so the payout queue has both passes and blocks in it.
                r.choices(("signed", "sent", "in_progress"), weights=(80, 12, 8))[0],
                f"ZOHO-LT-{g:06d}",
            )

    def bank_rows(self) -> Iterator[tuple[Any, ...]]:
        for i, n, g in self.trainer_indices():
            r = rng_for(f"bank:{i}:{n}")
            bank_name, ifsc_prefix = BANKS[g % len(BANKS)]
            yield (
                str(load_id(f"trainer:{i}:{n}")),
                f"{50000000000 + g * 7:012d}",
                f"{ifsc_prefix}0{r.randrange(100000, 999999):06d}",
                bank_name,
                CITIES[g % len(CITIES)],
                f"LoadTest Educator {g:06d}",
            )

    def deployment_plan(
        self,
    ) -> Iterator[tuple[str, str, str, str, dt.date, dt.date, str, str, Decimal]]:
        """(label, dep_id, trainer_id, batch_id, start, end, program_id, ptype, rate).

        One pass shared by deployments, work orders and attendance so all three
        agree on the same window and the same rate basis. A work order whose
        validity does not cover its deployment's attendance is a §7 gate failure,
        and generating them from separate passes is how that happens by accident.
        """
        for i in range(self.colleges):
            size = size_class(i)
            for j in range(size.programs):
                ptype = self.program_type(i, j)
                for k in range(size.batches_per_program):
                    for d in range(size.deployments_per_batch):
                        label = f"dep:{i}:{j}:{k}:{d}"
                        r = rng_for(label)
                        # Spread trainers across the college's batches without
                        # ever colliding on (trainer, batch), which is UNIQUE.
                        slot = (
                            j * size.batches_per_program * size.deployments_per_batch
                            + k * size.deployments_per_batch
                            + d
                        )
                        n = slot % size.trainers
                        start = WINDOW_START + dt.timedelta(days=r.randrange(0, 100))
                        end = min(start + dt.timedelta(days=r.randrange(30, 150)), WINDOW_END)
                        rate = r.choice(BCAP_RATES if ptype == "bCAP" else CRT_RATES)
                        yield (
                            label,
                            str(load_id(label)),
                            str(load_id(f"trainer:{i}:{n}")),
                            str(load_id(f"batch:{i}:{j}:{k}")),
                            start,
                            end,
                            str(load_id(f"program:{i}:{j}")),
                            ptype,
                            rate,
                        )

    def deployment_rows(self) -> Iterator[tuple[Any, ...]]:
        seen: set[tuple[str, str]] = set()
        for _, dep_id, trainer_id, batch_id, start, end, _, _, _ in self.deployment_plan():
            # `deployments_trainer_batch_key` is UNIQUE on
            # (trainer_id, batch_id, coalesce(start_date, ...)). Two deployments
            # of one batch land on different trainers by construction, but assert
            # it rather than trust it.
            if (trainer_id, batch_id) in seen:
                continue
            seen.add((trainer_id, batch_id))
            yield (dep_id, trainer_id, batch_id, start, end)

    def work_order_rows(self) -> Iterator[tuple[Any, ...]]:
        """One order per (trainer, program). R7: rate is Decimal end to end.

        `work_orders_unique_engagement` is UNIQUE on
        (trainer_id, program_id, valid_from), so a trainer deployed to two
        batches of one program gets ONE order, not two.
        """
        seen: set[tuple[str, str]] = set()
        for label, _, trainer_id, _, _, _, program_id, ptype, rate in self.deployment_plan():
            if (trainer_id, program_id) in seen:
                continue
            seen.add((trainer_id, program_id))
            r = rng_for(f"wo:{label}")
            yield (
                str(load_id(f"wo:{trainer_id}:{program_id}")),
                trainer_id,
                program_id,
                rate,
                "per_month" if ptype == "bCAP" else "per_day",
                WO_FROM,
                WO_TO,
                r.choices(("signed", "sent", "in_progress"), weights=(85, 10, 5))[0],
            )

    def pnl_rows(self) -> Iterator[tuple[Any, ...]]:
        """R7: every figure a Decimal, quantised to two places, never a float."""
        for i in range(self.colleges):
            size = size_class(i)
            for j in range(size.programs):
                r = rng_for(f"pnl:{i}:{j}")
                revenue = Decimal(r.randrange(400000, 4000000)).quantize(Decimal("0.01"))
                yield (
                    str(load_id(f"pnl:{i}:{j}")),
                    str(load_id(f"program:{i}:{j}")),
                    revenue,
                    (revenue * Decimal("0.42")).quantize(Decimal("0.01")),
                    Decimal(r.randrange(2000, 60000)).quantize(Decimal("0.01")),
                    (revenue * Decimal("0.55")).quantize(Decimal("0.01")),
                    (revenue * Decimal("0.35")).quantize(Decimal("0.01")),
                )

    def attendance_rows(self) -> Iterator[tuple[Any, ...]]:
        """ONE ROW PER DAY (CLAUDE.md §6). Never a wide D1-D31 layout.

        The mark distribution follows §5. For a CRT deployment a weekend carries
        no `P`, so it is simply absent from the table — the correct
        representation, and also what makes CRT's completeness check a hard
        block. For bCAP a weekend is marked HOLIDAY because the retainer absorbs
        it. ~7% of deployments are left with a three-day hole on purpose: an
        unmarked weekday is §5's trap (silently pays bCAP, silently underpays
        CRT) and the Delivery Monitor should have something real to find.
        """
        for label, dep_id, _, _, start, end, _, ptype, _ in self.deployment_plan():
            r = rng_for(f"att:{label}")
            bcap = ptype == "bCAP"
            gap_start = r.randrange(1, 25) if r.random() < 0.07 else None
            day = start
            index = 0
            while day <= end:
                index += 1
                if gap_start is not None and gap_start <= index < gap_start + 3:
                    day += dt.timedelta(days=1)
                    continue
                if day.weekday() >= 5:
                    if not bcap:
                        day += dt.timedelta(days=1)
                        continue
                    mark = "HOLIDAY"
                else:
                    mark = r.choices(("P", "A", "H", "HOLIDAY"), weights=(88, 5, 4, 3))[0]
                yield (
                    str(load_id(f"att:{label}:{day.isoformat()}")),
                    dep_id,
                    day,
                    mark,
                )
                day += dt.timedelta(days=1)


def plan_for(scale: int) -> Plan:
    return Plan(scale=scale, colleges=COLLEGES_PER_SCALE * scale)


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
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def say(message: str) -> None:
    print(f"  {message}", flush=True)


def all_counts(cur: psycopg.Cursor) -> dict[str, int]:
    """Row counts for every base table in `public`. The before/after proof."""
    cur.execute(
        "select table_name from information_schema.tables "
        "where table_schema = 'public' and table_type = 'BASE TABLE' order by 1"
    )
    out: dict[str, int] = {}
    for (table,) in cur.fetchall():
        cur.execute(f'select count(*) from public."{table}"')
        out[table] = cur.fetchone()[0]
    return out


def bulk_load(
    cur: psycopg.Cursor,
    table: str,
    columns: Sequence[str],
    rows: Iterator[tuple[Any, ...]] | Sequence[tuple[Any, ...]],
    *,
    conflict: str,
) -> tuple[int, float]:
    """`COPY` into a temp table, then `insert ... select ... on conflict`.

    Straight `COPY` into the target cannot express ON CONFLICT, and a chunked
    multi-row INSERT pays one network round trip per chunk — at ~700k attendance
    rows against a remote Supabase that is minutes of pure latency. The temp
    table is `on commit drop`: session-local, invisible to anything else, gone
    before the transaction ends. It is the only DDL this file issues.

    Returns (rows_streamed, seconds).
    """
    started = time.perf_counter()
    tmp = f"_lt_{table}"
    # `including defaults` matters: without it the temp table inherits NOT NULL
    # from `created_at` but not its `default now()`, and every COPY fails on the
    # first row. NOT `including constraints/indexes` — a duplicate must be
    # resolved by the ON CONFLICT on the real table, not rejected mid-stream.
    cur.execute(f"create temp table {tmp} (like public.{table} including defaults) on commit drop")
    collist = ", ".join(columns)
    streamed = 0
    with cur.copy(f"copy {tmp} ({collist}) from stdin") as copy:
        for row in rows:
            copy.write_row(row)
            streamed += 1
    cur.execute(f"insert into public.{table} ({collist}) select {collist} from {tmp} {conflict}")
    cur.execute(f"drop table {tmp}")
    return streamed, time.perf_counter() - started


#: (table, columns, plan attribute, conflict clause), in dependency order.
LOAD_STEPS: Final[tuple[tuple[str, tuple[str, ...], str, str], ...]] = (
    ("clusters", ("id", "name"), "cluster_rows", "on conflict (id) do nothing"),
    (
        "colleges",
        ("id", "cluster_id", "name", "city", "mou_status", "po_status"),
        "college_rows",
        "on conflict (id) do nothing",
    ),
    (
        "programs",
        ("id", "college_id", "type", "name", "start_date", "end_date", "stage"),
        "program_rows",
        "on conflict (id) do nothing",
    ),
    (
        "batches",
        ("id", "program_id", "name", "branch", "section", "passout_year", "expected_student_count"),
        "batch_rows",
        "on conflict (id) do nothing",
    ),
    (
        "students",
        ("id", "batch_id", "full_name", "roll_number"),
        "student_rows",
        "on conflict (id) do nothing",
    ),
    (
        "tasks",
        ("id", "program_id", "stage", "title", "status", "due_date", "waiting_on"),
        "task_rows",
        "on conflict (id) do nothing",
    ),
    (
        "trainers",
        ("id", "pan", "full_name", "email", "type", "work_order_status", "zoho_id"),
        "trainer_rows",
        "on conflict (id) do nothing",
    ),
    (
        "trainer_bank_accounts",
        ("trainer_id", "bank_account_number", "ifsc", "bank_name", "branch", "account_name"),
        "bank_rows",
        "on conflict (trainer_id) do nothing",
    ),
    (
        "deployments",
        ("id", "trainer_id", "batch_id", "start_date", "end_date"),
        "deployment_rows",
        "on conflict (id) do nothing",
    ),
    (
        "work_orders",
        (
            "id",
            "trainer_id",
            "program_id",
            "rate",
            "rate_basis",
            "valid_from",
            "valid_to",
            "status",
        ),
        "work_order_rows",
        "on conflict (id) do nothing",
    ),
    (
        "pnl",
        (
            "id",
            "program_id",
            "revenue",
            "trainer_cost",
            "travel_cost",
            "accrued_amount",
            "invoiced_amount",
        ),
        "pnl_rows",
        "on conflict (id) do nothing",
    ),
    (
        "trainer_attendance",
        ("id", "deployment_id", "mark_date", "mark"),
        "attendance_rows",
        "on conflict (id) do nothing",
    ),
)


# --- create -------------------------------------------------------------------


def create(env: dict[str, str], scale: int) -> None:
    plan = plan_for(scale)
    print(f"\nLoading scale {scale}x — {plan.colleges} colleges\n" + "-" * 70)

    print("\nAuth accounts")
    for user in LOAD_USERS:
        code, payload = admin_call(
            env,
            "/auth/v1/admin/users",
            method="POST",
            body={
                "id": str(user.id),
                "email": user.email,
                "password": LOAD_PASSWORD,
                "email_confirm": True,
                "user_metadata": {"role": user.role, "full_name": user.full_name},
            },
        )
        verb = (
            "created "
            if code in (200, 201)
            else ("exists  " if code in (409, 422) else f"FAILED {code} {payload}")
        )
        say(f"{verb} {user.email:32} {user.role}")

    timings: list[tuple[str, int, float]] = []
    overall = time.perf_counter()

    with psycopg.connect(env["DATABASE_URL"], connect_timeout=60) as conn, conn.cursor() as cur:
        # The signup trigger writes profiles.role from user_metadata, but an
        # account that already existed keeps whatever it had. Assert the persona
        # rather than assume the trigger ran the way this script wanted.
        for user in LOAD_USERS:
            cur.execute(
                "update public.profiles set role = %s, full_name = %s where id = %s",
                (user.role, user.full_name, str(user.id)),
            )

        print("\nBulk load (COPY -> temp -> insert ... on conflict)")
        print(f"  {'table':24} {'rows':>10} {'seconds':>9} {'rows/sec':>10}")
        for table, columns, attr, conflict in LOAD_STEPS:
            count, seconds = bulk_load(
                cur, table, columns, getattr(plan, attr)(), conflict=conflict
            )
            timings.append((table, count, seconds))
            rate = count / seconds if seconds else 0.0
            print(f"  {table:24} {count:>10,} {seconds:>9.2f} {rate:>10,.0f}", flush=True)

        print("\nReach")
        cur.execute(
            "insert into public.user_cluster_assignments (user_id, cluster_id) values (%s, %s) "
            "on conflict (user_id, cluster_id) do nothing",
            (str(load_id("user:sm")), str(load_id("cluster:0"))),
        )
        manager_reach = [i for i in MANAGER_COLLEGES if i < plan.colleges]
        for i in manager_reach:
            cur.execute(
                "insert into public.user_college_assignments (user_id, college_id) "
                "values (%s, %s) on conflict (user_id, college_id) do nothing",
                (str(load_id("user:mgr")), str(load_id(f"college:{i}"))),
            )
        cur.execute(
            "insert into public.user_college_assignments (user_id, college_id) "
            "values (%s, %s) on conflict (user_id, college_id) do nothing",
            (str(load_id("user:lde")), str(load_id(f"college:{LDE_COLLEGE}"))),
        )
        say(
            f"SM -> cluster:0 ({min(COLLEGES_PER_CLUSTER, plan.colleges)} colleges) · "
            f"MGR -> {len(manager_reach)} colleges · LDE -> college:0"
        )

        conn.commit()

    total = sum(c for _, c, _ in timings)
    elapsed = time.perf_counter() - overall
    print("\n" + "-" * 70)
    print(
        f"scale {scale}x: {total:,} rows in {elapsed:.1f}s "
        f"({total / elapsed:,.0f} rows/sec end to end)"
    )
    print(f"Sign in as: {LOAD_USERS[0].email} / {LOAD_PASSWORD}")
    print("Remove it all with:  python tools/load_data.py --delete")


# --- delete -------------------------------------------------------------------


def _id_space(plan: Plan) -> dict[str, list[str]]:
    """Every id this script could have written at `plan`'s scale.

    Computed, never discovered by query. That is the guarantee: a delete driven
    by a computed id set cannot widen to a row somebody else created, however
    the database looks when it runs.
    """
    deployment_ids = [d for _, d, *_ in plan.deployment_plan()]
    wo_ids: list[str] = []
    seen: set[tuple[str, str]] = set()
    for _, _, trainer_id, _, _, _, program_id, _, _ in plan.deployment_plan():
        if (trainer_id, program_id) in seen:
            continue
        seen.add((trainer_id, program_id))
        wo_ids.append(str(load_id(f"wo:{trainer_id}:{program_id}")))
    return {
        "deployments": deployment_ids,
        "work_orders": wo_ids,
        "trainers": [str(load_id(f"trainer:{i}:{n}")) for i, n, _ in plan.trainer_indices()],
        "programs": [
            str(load_id(f"program:{i}:{j}"))
            for i in range(plan.colleges)
            for j in range(size_class(i).programs)
        ],
        "batches": [
            str(load_id(f"batch:{i}:{j}:{k}"))
            for i in range(plan.colleges)
            for j in range(size_class(i).programs)
            for k in range(size_class(i).batches_per_program)
        ],
        "colleges": [str(load_id(f"college:{i}")) for i in range(plan.colleges)],
        "clusters": [str(load_id(f"cluster:{i}")) for i in range(plan.cluster_count())],
    }


def delete(env: dict[str, str], scale: int) -> None:
    """Remove every load-test row, in dependency order, by id.

    Defaults to the LARGEST scale's id space so a --delete after any rung is
    complete. Addressing an id that was never created is a no-op, and no
    statement here has a predicate that could reach a row this script did not
    write.
    """
    plan = plan_for(scale)
    print(f"\nDeleting load-test data (id space of scale {scale}x)\n" + "-" * 70)
    ids = _id_space(plan)

    started = time.perf_counter()
    with psycopg.connect(env["DATABASE_URL"], connect_timeout=60) as conn, conn.cursor() as cur:
        # Order matters. 0400 holds `trainers` down with RESTRICT from both
        # `deployments` and `work_orders` — deliberately, so a trainer with
        # history cannot vanish from under a payout. Clear the history first.
        steps: tuple[tuple[str, str, list[str]], ...] = (
            ("trainer_attendance", "deployment_id", ids["deployments"]),
            ("observations", "deployment_id", ids["deployments"]),
            ("deployments", "id", ids["deployments"]),
            ("work_orders", "id", ids["work_orders"]),
            ("remuneration_sheets", "trainer_id", ids["trainers"]),
            ("erm_sync_tasks", "trainer_id", ids["trainers"]),
            ("erm_sync_tasks", "program_id", ids["programs"]),
            ("trainer_bank_accounts", "trainer_id", ids["trainers"]),
            ("trainers", "id", ids["trainers"]),
            ("students", "batch_id", ids["batches"]),
            ("feedback", "batch_id", ids["batches"]),
            ("assessments", "batch_id", ids["batches"]),
            ("attendance_records", "batch_id", ids["batches"]),
            ("batches", "id", ids["batches"]),
            ("tasks", "program_id", ids["programs"]),
            ("pnl", "program_id", ids["programs"]),
            ("governance_reports", "program_id", ids["programs"]),
            ("program_documents", "program_id", ids["programs"]),
            ("comms_messages", "program_id", ids["programs"]),
            ("programs", "id", ids["programs"]),
            ("colleges", "id", ids["colleges"]),
            ("clusters", "id", ids["clusters"]),
        )
        for table, column, id_list in steps:
            if not id_list:
                continue
            try:
                cur.execute(
                    f"delete from public.{table} where {column} = any(%s::uuid[])", (id_list,)
                )
                if cur.rowcount:
                    say(f"{table:24} removed {cur.rowcount:,}")
            except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
                conn.rollback()
        conn.commit()

    # Auth users last. The cascade from auth.users takes profiles and both
    # assignment tables with it, so nothing above had to address them.
    print()
    for user in LOAD_USERS:
        code, _ = admin_call(env, f"/auth/v1/admin/users/{user.id}", method="DELETE")
        say(f"{'removed ' if code in (200, 204) else f'({code}) '}{user.email}")

    print("\n" + "-" * 70)
    print(
        f"Load-test data removed in {time.perf_counter() - started:.1f}s. "
        "Real rows were never addressed — deletion is by id."
    )


# --- status / counts ----------------------------------------------------------


def status(env: dict[str, str], scale: int) -> None:
    plan = plan_for(scale)
    ids = _id_space(plan)
    print(f"\nLoad-test data present? (id space of scale {scale}x)\n" + "-" * 70)
    checks: tuple[tuple[str, str, list[str]], ...] = (
        ("clusters", "id", ids["clusters"]),
        ("colleges", "id", ids["colleges"]),
        ("programs", "id", ids["programs"]),
        ("batches", "id", ids["batches"]),
        ("trainers", "id", ids["trainers"]),
        ("work_orders", "id", ids["work_orders"]),
        ("deployments", "id", ids["deployments"]),
        ("trainer_attendance", "deployment_id", ids["deployments"]),
        ("profiles", "id", [str(u.id) for u in LOAD_USERS]),
    )
    with psycopg.connect(env["DATABASE_URL"], connect_timeout=60) as conn, conn.cursor() as cur:
        for table, column, id_list in checks:
            cur.execute(
                f"select count(*) from public.{table} where {column} = any(%s::uuid[])", (id_list,)
            )
            say(f"{table:24} {cur.fetchone()[0]:>10,}  (id space {len(id_list):,})")
        cur.execute("select pg_size_pretty(pg_database_size(current_database()))")
        say(f"{'database size':24} {cur.fetchone()[0]:>10}")


def counts(env: dict[str, str], as_json: bool) -> None:
    with psycopg.connect(env["DATABASE_URL"], connect_timeout=60) as conn, conn.cursor() as cur:
        data = all_counts(cur)
        cur.execute("select pg_database_size(current_database())")
        size = cur.fetchone()[0]
    if as_json:
        print(json.dumps({"counts": data, "db_size_bytes": size}, indent=2))
        return
    print("\nRow counts — every base table in public\n" + "-" * 70)
    for table, n in data.items():
        say(f"{table:32} {n:>12,}")
    say(f"{'-- database size (MB)':32} {size / 1048576:>12,.1f}")


def show_plan() -> None:
    print(
        f"{'scale':>6} {'colleges':>9} {'clusters':>9} {'programs':>9} {'batches':>8} "
        f"{'students':>9} {'tasks':>8} {'trainers':>9} {'work_ord':>9} "
        f"{'deploys':>8} {'attendance':>11}"
    )
    for s in SCALES:
        p = plan_for(s)
        print(
            f"{s:>5}x {p.colleges:>9,} {p.cluster_count():>9,} {len(p.program_rows()):>9,} "
            f"{len(p.batch_rows()):>8,} {sum(1 for _ in p.student_rows()):>9,} "
            f"{sum(1 for _ in p.task_rows()):>8,} {sum(1 for _ in p.trainer_indices()):>9,} "
            f"{sum(1 for _ in p.work_order_rows()):>9,} "
            f"{sum(1 for _ in p.deployment_rows()):>8,} "
            f"{sum(1 for _ in p.attendance_rows()):>11,}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true")
    group.add_argument("--delete", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--counts", action="store_true", help="row counts, every public table")
    group.add_argument("--plan", action="store_true", help="planned row counts, no I/O")
    parser.add_argument(
        "--scale",
        type=int,
        default=None,
        help=f"one of {SCALES}. --create defaults to 1; --delete/--status default to {MAX_SCALE}.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable --counts")
    args = parser.parse_args()

    if args.plan:
        show_plan()
        return 0

    env = load_env()
    if args.create:
        scale = args.scale or 1
        if scale not in SCALES:
            sys.exit(f"--scale must be one of {SCALES}")
        create(env, scale)
    elif args.delete:
        delete(env, args.scale or MAX_SCALE)
    elif args.status:
        status(env, args.scale or MAX_SCALE)
    else:
        counts(env, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
