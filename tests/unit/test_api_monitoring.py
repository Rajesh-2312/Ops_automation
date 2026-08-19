"""`app/api/monitoring.py` — Phase 5's HTTP surface. Alert ceiling, walls in code.

The session is faked the way `test_api_reports.py` fakes it: it holds ORM
instances, answers `execute()` by the selected entity, does NOT evaluate WHERE
clauses, and RAISES on any write. Two properties of that fake are load-bearing
here:

* **Ignoring WHERE is what makes the R5 tests mean something.** The endpoint runs
  on a `BYPASSRLS` connection, so the SQL predicate is not the wall — the Python
  filter in `_reachable_programs()` is. A fake that honoured `WHERE` would pass
  these tests even if that filter were deleted. This one does not: with the
  filter removed, `test_a_college_outside_reach_yields_zero_rows` fails, which is
  exactly the assertion CLAUDE.md R5 asks for.
* **Raising on write asserts §8's ceiling for free.** Every test in this file
  would fail if any handler wrote a row, an audit event or an escalation record.

There is no `FakeLLM` here and no narrator override, on purpose: §8 says the
Escalation Engine is "deterministic SLA rules. Not LLM judgement", and a test
file that had to stub a model would be evidence the endpoint could call one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import monitoring
from app.core.security import Principal, get_principal
from app.db.models import (
    Batch,
    Cluster,
    College,
    Deployment,
    Profile,
    Program,
    ProgramDocument,
    Task,
    Trainer,
    TrainerAttendance,
    UserClusterAssignment,
    UserCollegeAssignment,
)
from app.db.session import get_session
from app.domain.enums import (
    AttendanceMark,
    DocStatus,
    Persona,
    ProgramStage,
    ProgramType,
    TaskCadence,
    TaskStatus,
)
from app.domain.risk import SlaCode, SlaMetric

COLLEGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROGRAM_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
BATCH_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
DEPLOYMENT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
TRAINER_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
USER_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
LDE_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")
MANAGER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CLUSTER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OTHER_COLLEGE_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")

ALERTS = "/monitoring/alerts"
RULES = "/monitoring/rules"

#: Every request pins `as_of`. The two services never read a clock, and neither
#: does the endpoint once this is bound — so every assertion below is about the
#: inputs and not about the day the suite runs.
AS_OF = dt.datetime(2026, 7, 20, 9, 0, tzinfo=dt.UTC)
JULY = {"period_start": "2026-07-01", "period_end": "2026-07-31", "as_of": AS_OF.isoformat()}


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    def __init__(self, *rows: Any) -> None:
        self.rows = list(rows)
        self.queries: list[str] = []

    async def get(self, model: type[Any], pk: Any) -> Any:
        self.queries.append(model.__name__)
        return next((r for r in self.rows if isinstance(r, model) and r.id == pk), None)

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        self.queries.append(entity.__name__)
        return FakeResult([r for r in self.rows if isinstance(r, entity)])

    def add(self, _obj: Any) -> None:  # pragma: no cover
        raise AssertionError("a monitoring endpoint tried to write (R3, R4, §8)")

    async def commit(self) -> None:  # pragma: no cover
        raise AssertionError("a monitoring endpoint tried to commit (R3, R4, §8)")


def a_principal(persona: Persona, *, reach: frozenset[uuid.UUID] | None = None) -> Principal:
    return Principal(
        user_id=USER_ID,
        persona=persona,
        college_ids=frozenset({COLLEGE_ID}) if reach is None else reach,
    )


def client(session: FakeSession, principal: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(monitoring.router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app)


def scenario(
    *,
    program_type: ProgramType = ProgramType.BCAP,
    marked_through: dt.date | None = dt.date(2026, 7, 20),
    last_marked_at: dt.datetime = dt.datetime(2026, 7, 20, 8, 0, tzinfo=dt.UTC),
    deployment_start: dt.date | None = None,
    overdue_task: bool = False,
    unsigned_document: bool = False,
    staff: bool = True,
    cluster_staff: bool = False,
) -> FakeSession:
    """One college, one program, one batch, one deployment, July marks 1..N.

    `marked_through` is the last day carrying a `P`. Everything after it up to
    `elapsed_through` (the 20th, from `AS_OF`) is UNMARKED, which is the §5 branch
    the tests below turn on.
    """
    rows: list[Any] = [
        Cluster(id=CLUSTER_ID, name="Andhra"),
        College(id=COLLEGE_ID, name="Malineni Lakshmaiah", cluster_id=CLUSTER_ID),
        Program(
            id=PROGRAM_ID,
            college_id=COLLEGE_ID,
            type=program_type,
            name="Delivery 2026",
            stage=ProgramStage.ACTIVE_MONITORING,
            start_date=dt.date(2026, 6, 1),
            end_date=dt.date(2026, 12, 31),
        ),
        Batch(id=BATCH_ID, program_id=PROGRAM_ID, name="CSE-A"),
        Deployment(
            id=DEPLOYMENT_ID,
            trainer_id=TRAINER_ID,
            batch_id=BATCH_ID,
            start_date=deployment_start,
        ),
        Trainer(
            id=TRAINER_ID,
            pan="VEMAP1234K",
            full_name="VEMA PRUDHVI SAI",
            type="freelance",
            work_order_status=DocStatus.SIGNED,
            erm_status="synced",
        ),
    ]

    if marked_through is not None:
        day = dt.date(2026, 7, 1)
        while day <= marked_through:
            rows.append(
                TrainerAttendance(
                    id=uuid.uuid4(),
                    deployment_id=DEPLOYMENT_ID,
                    mark_date=day,
                    mark=AttendanceMark.PRESENT,
                    marked_at=last_marked_at,
                )
            )
            day += dt.timedelta(days=1)

    if overdue_task:
        rows.append(
            Task(
                id=uuid.uuid4(),
                program_id=PROGRAM_ID,
                stage=ProgramStage.ACTIVE_MONITORING,
                title="Collect the signed tracksheet",
                status=TaskStatus.PENDING,
                cadence=TaskCadence.ONE_TIME,
                # 96 h before AS_OF's midnight boundary — past the 72 h rung.
                due_date=dt.date(2026, 7, 15),
            )
        )
    if unsigned_document:
        rows.append(
            ProgramDocument(
                id=uuid.uuid4(),
                program_id=PROGRAM_ID,
                category="contracts",
                name="Work order",
                status=DocStatus.SENT,
                due_date=dt.date(2026, 7, 1),
            )
        )

    if staff:
        rows += [
            Profile(id=LDE_ID, role=Persona.LDE_EXECUTIVE, is_admin=False, full_name="Priya"),
            UserCollegeAssignment(user_id=LDE_ID, college_id=COLLEGE_ID),
        ]
    if cluster_staff:
        rows += [
            Profile(id=MANAGER_ID, role=Persona.MANAGER, is_admin=False, full_name="Rajesh"),
            UserClusterAssignment(user_id=MANAGER_ID, cluster_id=CLUSTER_ID),
        ]
    return FakeSession(*rows)


def feed(session: FakeSession, principal: Principal, **params: Any) -> dict[str, Any]:
    response = client(session, principal).get(ALERTS, params={**JULY, **params})
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- §4 / R5: who may read what ----------------------------------------------


@pytest.mark.parametrize("persona", [Persona.TRAINER, Persona.COLLEGE])
@pytest.mark.parametrize("url", [ALERTS, RULES])
def test_the_monitor_is_internal_only(persona: Persona, url: str) -> None:
    """§8: the Delivery Monitor's ceiling is "Alert (INTERNAL ONLY)".

    A trainer is a record and not a user at all (§4), and a College login sees
    published artifacts only. Both are refused before a row is read, which is also
    what keeps `build_alert()`'s external-audience guard unreachable from here.
    """
    response = client(scenario(), a_principal(persona)).get(url, params=JULY)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "persona", [Persona.SENIOR_MANAGER, Persona.MANAGER, Persona.LDE_EXECUTIVE]
)
def test_every_internal_persona_may_read_the_feed(persona: Persona) -> None:
    """Including an LDE Executive: attendance and batches are their day (§4)."""
    body = feed(scenario(), a_principal(persona))
    assert body["audience"] == persona.value
    assert body["program_count"] == 1


def test_a_college_outside_reach_yields_zero_rows() -> None:
    """R5, the canonical shape: a forbidden read returns ZERO ROWS, not an error.

    The fake session ignores `WHERE`, so this passes only because
    `_reachable_programs()` filters in Python. On a `BYPASSRLS` connection that
    filter is the entire wall.
    """
    body = feed(scenario(), a_principal(Persona.MANAGER, reach=frozenset({OTHER_COLLEGE_ID})))
    assert body["programs"] == []
    assert body["program_count"] == 0
    assert body["deployment_count"] == 0
    assert body["escalation_count"] == 0


def test_a_manager_with_no_assignments_sees_nothing() -> None:
    """Deny by default: reach is assignments, never persona (§4)."""
    body = feed(scenario(), a_principal(Persona.MANAGER, reach=frozenset()))
    assert body["programs"] == []


def test_a_named_program_outside_reach_is_refused() -> None:
    """A caller who asserts one program is told no, rather than handed an empty list."""
    session = scenario()
    principal = a_principal(Persona.MANAGER, reach=frozenset({OTHER_COLLEGE_ID}))
    response = client(session, principal).get(
        ALERTS, params={**JULY, "program_id": str(PROGRAM_ID)}
    )
    assert response.status_code == 403


def test_a_named_college_outside_reach_is_refused() -> None:
    session = scenario()
    principal = a_principal(Persona.MANAGER, reach=frozenset({OTHER_COLLEGE_ID}))
    response = client(session, principal).get(
        ALERTS, params={**JULY, "college_id": str(COLLEGE_ID)}
    )
    assert response.status_code == 403


def test_an_unknown_program_is_a_404() -> None:
    response = client(scenario(), a_principal(Persona.MANAGER)).get(
        ALERTS, params={**JULY, "program_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


# --- the commercials wall: nothing here is behind it, and nothing leaks --------


def test_no_commercial_figure_reaches_an_lde_executive() -> None:
    """§4: "An LDE Executive gets ZERO ROWS from pnl, remuneration, invoices and
    work-order rates."

    The monitoring feed is not behind `can_see_commercials()` precisely because it
    carries no such figure. This asserts the premise rather than trusting it: if a
    rate, an amount, an invoice number or a PAN is ever added to an anomaly or an
    escalation, this fails and the endpoint must gain `require_commercials()`.
    """
    raw = client(scenario(overdue_task=True), a_principal(Persona.LDE_EXECUTIVE)).get(
        ALERTS, params=JULY
    )
    assert raw.status_code == 200
    text = raw.text.lower()
    for forbidden in ("pan", "invoice", "net_amount", "rate_per_day", "tds", "remuneration", "₹"):
        assert forbidden not in text, f"a commercial token reached the feed: {forbidden}"


def test_the_feed_never_queries_a_commercial_table() -> None:
    """Belt to the braces above: the wall holds even if a field is renamed."""
    session = scenario(overdue_task=True, unsigned_document=True)
    feed(session, a_principal(Persona.LDE_EXECUTIVE))
    for table in ("RemunerationSheet", "WorkOrder", "Pnl", "TrainerBankAccount"):
        assert table not in session.queries


# --- CLAUDE.md §5: the CRT / bCAP asymmetry survives the boundary -------------


def test_an_unmarked_crt_day_is_critical_and_escalates_immediately() -> None:
    """§5: CRT counts payable days UP from `P`, so one unmarked day underpays.

    The detector says CRITICAL and `SlaCode.ATTENDANCE_UNMARKED_CRT` fires at a
    threshold of one. Neither number is in `app/api/monitoring.py`.
    """
    body = feed(
        scenario(program_type=ProgramType.CRT, marked_through=dt.date(2026, 7, 18)),
        a_principal(Persona.MANAGER),
    )
    program = body["programs"][0]
    anomalies = program["deployments"][0]["anomalies"]
    unmarked = next(a for a in anomalies if a["code"] == "attendance_unmarked_days")
    assert unmarked["severity"] == "critical"
    assert unmarked["detail"]["unmarked_days"] == "2"

    codes = [e["code"] for e in program["escalations"]]
    assert SlaCode.ATTENDANCE_UNMARKED_CRT.value in codes
    assert SlaCode.ATTENDANCE_UNMARKED_BCAP.value not in codes


def test_the_same_gap_on_bcap_is_a_warning_and_does_not_escalate_yet() -> None:
    """§5: bCAP counts DOWN from period length, so the exposure accumulates.

    Two unmarked days is below the bCAP rule's threshold of three. The opposite
    program type, the identical attendance — and a different answer, which is the
    whole point of `SlaRule.program_type`.
    """
    body = feed(
        scenario(program_type=ProgramType.BCAP, marked_through=dt.date(2026, 7, 18)),
        a_principal(Persona.MANAGER),
    )
    program = body["programs"][0]
    unmarked = next(
        a for a in program["deployments"][0]["anomalies"] if a["code"] == "attendance_unmarked_days"
    )
    assert unmarked["severity"] == "info"
    assert [e["code"] for e in program["escalations"]] == []


def test_a_clean_tracksheet_produces_no_anomaly_and_a_low_band() -> None:
    body = feed(scenario(), a_principal(Persona.MANAGER))
    deployment = body["programs"][0]["deployments"][0]
    assert deployment["anomalies"] == []
    assert deployment["band"] == "low"
    assert deployment["score"] == "0"
    assert body["band_counts"]["low"] == 1


# --- the window is clipped to the deployment ---------------------------------


def test_a_deployment_starting_mid_period_is_not_blamed_for_earlier_days() -> None:
    """A trainer who started on the 18th did not fail to mark the 1st.

    Without the clip this deployment shows seventeen unmarked days and a CRITICAL
    band on its first week, which is how an alert feed becomes noise.
    """
    body = feed(
        scenario(
            program_type=ProgramType.CRT,
            deployment_start=dt.date(2026, 7, 18),
            marked_through=dt.date(2026, 7, 20),
        ),
        a_principal(Persona.MANAGER),
    )
    deployment = body["programs"][0]["deployments"][0]
    assert deployment["period_start"] == "2026-07-18"
    assert deployment["anomalies"] == []


def test_the_window_stops_at_as_of_not_at_period_end() -> None:
    """The 21st to the 31st have not happened yet on the 20th."""
    body = feed(scenario(), a_principal(Persona.MANAGER))
    deployment = body["programs"][0]["deployments"][0]
    assert deployment["period_end"] == "2026-07-31"
    assert deployment["elapsed_through"] == "2026-07-20"
    assert deployment["anomalies"] == []


# --- escalation: deterministic, routed, and explained from the rule row --------


def test_a_severely_overdue_task_escalates_to_a_manager_with_its_arithmetic() -> None:
    body = feed(scenario(overdue_task=True), a_principal(Persona.MANAGER))
    escalations = body["programs"][0]["escalations"]
    severe = next(e for e in escalations if e["code"] == SlaCode.TASK_SEVERELY_OVERDUE.value)
    assert severe["metric"] == SlaMetric.TASK_HOURS_OVERDUE.value
    assert severe["threshold"] == "72"
    assert Decimal(severe["measured"]) > Decimal(72)
    # The reason is generated from the rule row, never written about it.
    assert "task_hours_overdue is above 72" in severe["reason"]
    assert severe["requested_tier"] == "manager"


def test_an_escalation_climbs_when_its_rung_is_unstaffed() -> None:
    """§4: "An unstaffed rung climbs, it does not drop."

    Only a Manager is assigned (via the cluster), so the LDE-tier rule lands on
    the Manager and says so. A signal that landed on nobody would have been lost.
    """
    session = scenario(overdue_task=True, staff=False, cluster_staff=True)
    body = feed(session, a_principal(Persona.MANAGER))
    escalations = body["programs"][0]["escalations"]
    lde_rule = next(e for e in escalations if e["code"] == SlaCode.TASK_OVERDUE.value)
    assert lde_rule["requested_tier"] == "lde_executive"
    assert lde_rule["resolved_tier"] == "manager"
    assert lde_rule["climbed"] is True
    assert lde_rule["recipient_count"] == 1
    assert lde_rule["unrouted"] is False


def test_an_unstaffed_college_reports_unrouted_rather_than_failing() -> None:
    """A college nobody is assigned to is an ops gap, surfaced not raised."""
    session = scenario(overdue_task=True, staff=False)
    body = feed(session, a_principal(Persona.MANAGER))
    lde_rule = next(
        e for e in body["programs"][0]["escalations"] if e["code"] == SlaCode.TASK_OVERDUE.value
    )
    assert lde_rule["unrouted"] is True
    assert lde_rule["recipient_count"] == 0
    assert lde_rule["resolved_tier"] == "senior_manager"


def test_recipient_ids_are_never_exported() -> None:
    """The feed reports a count and a tier, the way `escalation.routed` does."""
    raw = client(scenario(overdue_task=True), a_principal(Persona.MANAGER)).get(ALERTS, params=JULY)
    assert str(LDE_ID) not in raw.text


def test_an_unsigned_document_escalates_on_its_own_rule() -> None:
    body = feed(scenario(unsigned_document=True), a_principal(Persona.MANAGER))
    codes = [e["code"] for e in body["programs"][0]["escalations"]]
    assert SlaCode.DOCUMENT_UNSIGNED.value in codes


def test_a_metric_nobody_measured_never_fires() -> None:
    """`engine.py`: "not measured is not zero". Both are reported as unmeasured."""
    body = feed(scenario(), a_principal(Persona.MANAGER))
    unmeasured = body["programs"][0]["unmeasured_metrics"]
    assert SlaMetric.PAYOUT_HOURS_BLOCKED.value in unmeasured
    assert SlaMetric.ESCALATION_HOURS_UNACKNOWLEDGED.value in unmeasured
    assert body["escalation_count"] == 0


def test_two_identical_requests_produce_an_identical_body() -> None:
    """§8's determinism promise, asserted rather than described."""
    first = client(scenario(overdue_task=True), a_principal(Persona.MANAGER)).get(
        ALERTS, params=JULY
    )
    second = client(scenario(overdue_task=True), a_principal(Persona.MANAGER)).get(
        ALERTS, params=JULY
    )
    assert first.json() == second.json()


# --- the alert itself ---------------------------------------------------------


def test_the_alert_text_only_restates_figures_it_was_given() -> None:
    """R1: every figure in the rendered lines came out of an anomaly's `detail`."""
    body = feed(
        scenario(program_type=ProgramType.CRT, marked_through=dt.date(2026, 7, 18)),
        a_principal(Persona.MANAGER),
    )
    deployment = body["programs"][0]["deployments"][0]
    assert deployment["headline"].startswith("Delivery risk ")
    assert deployment["lines"]
    for anomaly in deployment["anomalies"]:
        assert any(anomaly["message"] in line for line in deployment["lines"])


def test_marking_that_stopped_is_its_own_anomaly() -> None:
    """`attendance_marking_stale` predicts the gap `attendance_unmarked_days` reports."""
    body = feed(
        scenario(
            marked_through=dt.date(2026, 7, 10),
            last_marked_at=dt.datetime(2026, 7, 10, 8, 0, tzinfo=dt.UTC),
        ),
        a_principal(Persona.MANAGER),
    )
    codes = [a["code"] for a in body["programs"][0]["deployments"][0]["anomalies"]]
    assert "attendance_marking_stale" in codes
    codes_escalated = [e["code"] for e in body["programs"][0]["escalations"]]
    assert SlaCode.ATTENDANCE_MARKING_STALE.value in codes_escalated


def test_a_program_score_is_the_worst_deployment_not_the_sum() -> None:
    body = feed(
        scenario(program_type=ProgramType.CRT, marked_through=dt.date(2026, 7, 18)),
        a_principal(Persona.MANAGER),
    )
    program = body["programs"][0]
    assert program["score"] == max(d["score"] for d in program["deployments"])


# --- the rule table -----------------------------------------------------------


def test_the_rule_table_is_served_in_table_order() -> None:
    response = client(scenario(), a_principal(Persona.MANAGER)).get(RULES)
    assert response.status_code == 200
    body = response.json()
    assert [r["code"] for r in body["rules"]] == [r.code.value for r in monitoring.DEFAULT_RULES]


def test_the_rule_table_says_which_rules_cannot_currently_fire() -> None:
    """A silent rule and a passing rule look identical without this."""
    body = client(scenario(), a_principal(Persona.MANAGER)).get(RULES).json()
    by_code = {r["code"]: r for r in body["rules"]}
    assert by_code[SlaCode.PAYOUT_BLOCKED.value]["is_measured"] is False
    assert by_code[SlaCode.ESCALATION_UNACKNOWLEDGED.value]["is_measured"] is False
    assert by_code[SlaCode.TASK_OVERDUE.value]["is_measured"] is True
    assert SlaMetric.PAYOUT_HOURS_BLOCKED.value in body["unmeasured_metrics"]


def test_a_climbing_rule_keeps_its_null_tier() -> None:
    """`tier=None` is "one rung above current" — a resolution rule, not a tier."""
    body = client(scenario(), a_principal(Persona.MANAGER)).get(RULES).json()
    by_code = {r["code"]: r for r in body["rules"]}
    assert by_code[SlaCode.ESCALATION_UNACKNOWLEDGED.value]["tier"] is None
    assert by_code[SlaCode.TASK_OVERDUE.value]["tier"] == "lde_executive"


def test_the_rule_table_is_read_only() -> None:
    """R3/R4: no rule is editable over HTTP, and no escalation is actionable."""
    session = scenario(overdue_task=True)
    app_client = client(session, a_principal(Persona.MANAGER))
    for method, url in (
        ("post", RULES),
        ("put", RULES),
        ("delete", RULES),
        ("post", ALERTS),
        ("patch", ALERTS),
    ):
        assert getattr(app_client, method)(url).status_code == 405


def test_no_route_can_send_acknowledge_or_release() -> None:
    """§8 ceiling, asserted over the router rather than over one handler.

    Every route is a GET, and no path names an action. A `send_`/`acknowledge`
    route added later fails here rather than in review.
    """
    for route in monitoring.router.routes:
        methods = getattr(route, "methods", set())
        assert methods <= {"GET", "HEAD"}, f"{route} exposes {methods}"
        path = getattr(route, "path", "")
        for verb in ("send", "notify", "release", "acknowledge", "resolve", "approve", ":"):
            assert verb not in path


# --- period handling ----------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"period_start": "2026-07-31", "period_end": "2026-07-01"},
        {"period_start": "2026-01-01", "period_end": "2026-12-31"},
        {"period_start": "2026-07-01"},
        {"period_end": "2026-07-31"},
    ],
)
def test_an_unusable_window_is_refused(params: dict[str, str]) -> None:
    response = client(scenario(), a_principal(Persona.MANAGER)).get(
        ALERTS, params={"as_of": AS_OF.isoformat(), **params}
    )
    assert response.status_code == 422


def test_the_window_defaults_to_the_month_of_as_of() -> None:
    """§6: the calendar month is the period a payout cycle is cut on."""
    response = client(scenario(), a_principal(Persona.MANAGER)).get(
        ALERTS, params={"as_of": AS_OF.isoformat()}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["period_start"] == "2026-07-01"
    assert body["period_end"] == "2026-07-31"


def test_a_naive_as_of_is_read_as_utc() -> None:
    """§11: timestamps are UTC in the DB; nothing here silently adopts local time."""
    response = client(scenario(), a_principal(Persona.MANAGER)).get(
        ALERTS, params={"as_of": "2026-07-20T09:00:00"}
    )
    assert response.status_code == 200
    assert response.json()["as_of"] == "2026-07-20T09:00:00Z"
