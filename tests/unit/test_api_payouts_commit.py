"""`POST /payouts/commit` — the write that makes a payout exist.

Everything else on the payout surface computes and forgets. This endpoint is the
only thing in the system that writes a `remuneration_sheets` row, issues an
invoice number and opens the R4 lifecycle, so the properties worth pinning are
about *persistence* rather than about arithmetic:

* **Nothing persists unless §7 allows it.** A blocking gate and a warning without
  a stated reason both refuse, and the refusal is asserted as "the session was
  never written to" rather than merely "the status code was 409" — a 409 raised
  after `session.add()` would still be a defect.
* **The invoice number is generated, never accepted, and its fiscal year comes
  from the payout month.** §6, and the February case is included because that is
  the one the April boundary gets wrong.
* **Committing twice does not pay twice.** One sheet, one number, `created=false`
  on the replay; a recomputation that disagrees with the stored row refuses.
* **R5 closes before any query.** A trainer must never commit their own payout —
  that is the payee authorising their own payment — and an LDE Executive must
  never see the row at all.
* **R2 end to end.** Both CLAUDE.md §6 fixtures are committed over HTTP and the
  persisted row is asserted to the rupee.

The scenario builder, the principals and the fake session all come from
`test_api_payouts.py` so the two suites cannot drift about what a payout looks
like. `CommitSession` widens that session in exactly one way: writing is allowed,
and recorded.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import payouts
from app.core.audit import AuditEvent, AuditWriter, get_audit_writer
from app.core.security import Principal, get_principal
from app.db.models import ArtifactVersion, RemunerationSheet
from app.db.session import get_session
from app.domain.enums import (
    ArtifactState,
    ArtifactType,
    AttendanceMark,
    DocStatus,
    Persona,
    RateBasis,
    ValidationCode,
)
from tests.unit.test_api_payouts import (
    DEPLOYMENT_ID,
    PAN,
    PERIOD_END,
    PERIOD_START,
    PROGRAM_ID,
    TRAINER_ID,
    FakeSession,
    _bank_account,
    _work_order,
    principal,
    scenario,
)

COMMIT_URL = "/payouts/commit"


# --- the session, now allowed to write ----------------------------------------


class CommitSession(FakeSession):
    """`FakeSession` with the write ban lifted and replaced by a recorder.

    `added` and `commits` are what the refusal tests assert on: "did not persist"
    means both are empty, which is stronger than checking a status code and is
    the only way to catch a row that was added and then abandoned without a
    commit — in a real transaction that row is still visible to the rest of the
    unit of work.
    """

    def __init__(self, *rows: Any) -> None:
        super().__init__(*rows)
        self.added: list[Any] = []
        self.commits = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self.rows.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    def sheets(self) -> list[RemunerationSheet]:
        return [row for row in self.added if isinstance(row, RemunerationSheet)]

    def versions(self) -> list[ArtifactVersion]:
        return [row for row in self.added if isinstance(row, ArtifactVersion)]


class TransactionalAudit(AuditWriter):
    """Records `write_within()` calls and asserts nothing uses `write()` here.

    §11 and `app/core/audit.py`: a money transition must be atomic with its audit
    row. If the endpoint ever downgrades to the best-effort path this fails
    loudly rather than passing with an audit row that can silently vanish.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:  # pragma: no cover
        raise AssertionError("a money transition used best-effort write() (§11)")

    async def write_within(self, session: Any, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def audit() -> TransactionalAudit:
    return TransactionalAudit()


@pytest.fixture
def client(audit: TransactionalAudit) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(payouts.router)
    app.dependency_overrides[get_audit_writer] = lambda: audit
    with TestClient(app) as test_client:
        yield test_client


def commit_session(**kwargs: Any) -> CommitSession:
    """The §6 fixture-one scenario, on a writable session.

    Rails are installed by default: without them §7 blocks on
    `bank_account_missing` and no payout could ever be committed, which would
    make every test here a test of the refusal path.
    """
    kwargs.setdefault("bank", "default")
    base = scenario(**kwargs)
    return CommitSession(*base.rows)


def commit(
    client: TestClient,
    session: CommitSession,
    caller: Principal | None = None,
    **body: Any,
) -> Any:
    client.app.dependency_overrides[get_session] = lambda: session
    client.app.dependency_overrides[get_principal] = lambda: caller or principal(Persona.MANAGER)
    payload: dict[str, Any] = {
        "deployment_id": str(DEPLOYMENT_ID),
        "period_start": PERIOD_START.isoformat(),
        "period_end": PERIOD_END.isoformat(),
        "ta_da": "100",
    }
    payload.update(body)
    return client.post(COMMIT_URL, json=payload)


def _committed_sheet(
    *,
    net: Decimal = Decimal("14035"),
    invoice_no: str = "VEMA/26-27/JUL1",
    period_start: dt.date = PERIOD_START,
    period_end: dt.date = PERIOD_END,
    seq: int = 1,
) -> RemunerationSheet:
    """A row as `commit_payout()` would have written it on an earlier request."""
    return RemunerationSheet(
        id=uuid.uuid4(),
        trainer_id=TRAINER_ID,
        program_id=PROGRAM_ID,
        period_start=period_start,
        period_end=period_end,
        net_amount=net,
        currency="INR",
        payout_status=DocStatus.IN_PROGRESS,
        invoice_pan=PAN,
        invoice_fy="26-27",
        invoice_month="JUL",
        invoice_seq=seq,
        invoice_no=invoice_no,
        created_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
    )


# =============================================================================
# R5 — who may commit
# =============================================================================


@pytest.mark.parametrize("persona", [Persona.LDE_EXECUTIVE, Persona.COLLEGE, Persona.TRAINER])
def test_commit_is_refused_before_any_query(client, persona) -> None:
    """§4 and R5, with the ordering that makes them mean anything.

    The LDE Executive and the College login are outside the commercials wall. The
    trainer is inside their own payout for a *read* — `POST /payouts/preview`
    lets them see it — and still refused here, because committing is the act that
    creates the payable artifact and a payee must not authorise their own
    payment.

    `queries == []` is the real assertion: on a BYPASSRLS connection the refusal
    has to land before the first SELECT, or the rate has already been read.
    """
    session = commit_session()
    response = commit(client, session, principal(persona))

    assert response.status_code == 403
    assert session.queries == []
    assert session.added == []
    assert session.commits == 0


def test_a_manager_without_reach_cannot_commit(client) -> None:
    """The wall and the scope are separate conjuncts, as in 0700_finance.sql."""
    session = commit_session()
    response = commit(client, session, principal(Persona.MANAGER, reach=False))

    assert response.status_code == 403
    assert session.added == []


@pytest.mark.parametrize("persona", [Persona.MANAGER, Persona.SENIOR_MANAGER])
def test_the_two_commercial_personas_may_commit(client, persona) -> None:
    assert commit(client, commit_session(), principal(persona)).status_code == 201


# =============================================================================
# §7 — a blocked payout does not persist
# =============================================================================


def test_a_blocking_gate_refuses_and_writes_nothing(client, audit) -> None:
    """§7's first gate: no signed work order, no payout.

    Asserted three ways because only the third is load-bearing: the status, the
    named code, and — the one that matters — that nothing reached the session. A
    payout row written for a trainer with no signed work order is money the
    system cannot defend, whatever HTTP status accompanied it.
    """
    session = commit_session(work_order=_work_order(status=DocStatus.SENT))
    response = commit(client, session)

    assert response.status_code == 409
    assert ValidationCode.WORK_ORDER_MISSING.value in response.json()["detail"]
    assert session.added == []
    assert session.commits == 0
    assert audit.events == []


def test_a_malformed_pan_cannot_be_committed(client) -> None:
    """No invoice number can be seeded from it (§6), so §7 blocks and nothing lands."""
    session = commit_session(trainer_pan="NOTAPAN")
    response = commit(client, session)

    assert response.status_code == 409
    assert session.added == []


def test_a_warning_without_a_stated_reason_is_refused(client) -> None:
    """§7: a warning "requires a stated reason". A missing one is a refusal.

    Built from `reimbursement_without_payable_days` — TA&DA claimed over a period
    with no payable day. bCAP counts DOWN, so an all-absent period is the way to
    reach zero payable days while keeping every other gate clean.
    """
    session = commit_session(marks=_all_absent())
    response = commit(client, session)

    assert response.status_code == 409
    assert ValidationCode.REIMBURSEMENT_WITH_ZERO_PAYABLE_DAYS.value in response.json()["detail"]
    assert session.added == []


def test_the_same_warning_with_a_reason_commits(client) -> None:
    """`ValidationReport.can_submit()` is the single predicate — not a reimplementation."""
    session = commit_session(marks=_all_absent())
    response = commit(
        client,
        session,
        stated_reasons={
            ValidationCode.REIMBURSEMENT_WITH_ZERO_PAYABLE_DAYS.value: "Travelled; college shut",
        },
    )

    assert response.status_code == 201, response.json()
    assert len(session.sheets()) == 1


def test_a_blank_reason_does_not_count(client) -> None:
    """ "Requires a stated reason" — a whitespace string is how that becomes a formality."""
    session = commit_session(marks=_all_absent())
    response = commit(
        client,
        session,
        stated_reasons={ValidationCode.REIMBURSEMENT_WITH_ZERO_PAYABLE_DAYS.value: "   "},
    )

    assert response.status_code == 409
    assert session.added == []


def _all_absent() -> dict[dt.date, AttendanceMark]:
    days = (PERIOD_END - PERIOD_START).days + 1
    return {PERIOD_START + dt.timedelta(days=n): AttendanceMark.ABSENT for n in range(days)}


# =============================================================================
# §6 — the invoice number
# =============================================================================


def test_the_invoice_number_has_the_section_six_shape(client) -> None:
    """`{PAN[0:4]}/{FY}/{MON}{seq}` — VEMA, FY 26-27 for July 2026, JUL1."""
    session = commit_session()
    response = commit(client, session)

    assert response.status_code == 201
    assert response.json()["invoice_number"] == "VEMA/26-27/JUL1"

    sheet = session.sheets()[0]
    assert (sheet.invoice_pan, sheet.invoice_fy, sheet.invoice_month, sheet.invoice_seq) == (
        PAN,
        "26-27",
        "JUL",
        1,
    )
    assert sheet.invoice_issued_at is not None


def test_the_fiscal_year_derives_from_the_payout_month_not_today(client) -> None:
    """§6: FY runs April-March, off the PAYOUT month.

    February 2026 is FY 25-26 — the previous fiscal year — while the July payouts
    everywhere else in this file are 26-27. Deriving from `date.today()` or from
    the period end would pass every other test in this suite and fail this one.
    """
    start, end = dt.date(2026, 2, 1), dt.date(2026, 2, 28)
    session = commit_session(
        marks=_present(start, end),
        # The order has to cover the period it is paying, or §7 blocks first.
        work_order=_work_order(valid_from=dt.date(2025, 4, 1), valid_to=dt.date(2026, 3, 31)),
    )
    response = commit(
        client,
        session,
        period_start=start.isoformat(),
        period_end=end.isoformat(),
    )

    assert response.status_code == 201, response.json()
    assert response.json()["invoice_number"] == "VEMA/25-26/FEB1"


def test_the_sequence_steps_past_the_numbers_already_issued(client) -> None:
    """`max(existing) + 1`, chosen inside the committing transaction (§6).

    The earlier row is for a different period, so it is not the idempotency case —
    it is a second engagement in the same month, which is exactly what `seq`
    exists to distinguish.
    """
    earlier = _committed_sheet(
        period_start=dt.date(2026, 7, 1),
        period_end=dt.date(2026, 7, 25),
        invoice_no="VEMA/26-27/JUL1",
        seq=1,
    )
    session = commit_session(sheets=[earlier])
    response = commit(client, session)

    assert response.status_code == 201
    assert response.json()["invoice_number"] == "VEMA/26-27/JUL2"


def test_an_invoice_number_supplied_by_the_caller_is_a_422(client) -> None:
    """Generate, never accept: `extra="forbid"` refuses the field outright."""
    session = commit_session()
    response = commit(client, session, invoice_number="ANSP/25-26/JULY")

    assert response.status_code == 422
    assert session.added == []


# =============================================================================
# Idempotency
# =============================================================================


def test_committing_twice_does_not_issue_a_second_number(client) -> None:
    """The replay: same trainer, same period, same recomputed net.

    200 rather than 201, `created=false`, the ORIGINAL invoice number, and not a
    single row added. The unique index on
    `(trainer_id, program_id, period_start, period_end)` would have caught a
    second insert — but at the point where a number had already been burned.
    """
    existing = _committed_sheet()
    session = commit_session(sheets=[existing])
    response = commit(client, session)

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is False
    assert body["sheet_id"] == str(existing.id)
    assert body["invoice_number"] == "VEMA/26-27/JUL1"
    assert session.added == []
    assert session.commits == 0


def test_a_replay_reports_where_the_artifact_actually_got_to(client) -> None:
    """A sheet approved since it was committed reports APPROVED, not DRAFT.

    Answering DRAFT on a replay would invite somebody to submit an already
    approved payout for approval a second time.
    """
    existing = _committed_sheet()
    version = ArtifactVersion(
        id=uuid.uuid4(),
        artifact_type=ArtifactType.REMUNERATION_SHEET,
        artifact_id=existing.id,
        version=2,
        state=ArtifactState.APPROVED,
        content_hash="a" * 64,
        created_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
    )
    session = commit_session(sheets=[existing])
    session.rows.append(version)

    body = commit(client, session).json()
    assert body["artifact_state"] == ArtifactState.APPROVED.value
    assert body["artifact_version"] == 2


def test_a_recomputation_that_disagrees_with_the_stored_row_refuses(client) -> None:
    """Attendance changed under a committed payout: 409, both figures named.

    Neither silent option is acceptable. Returning the stale row would answer
    "committed" for a number that is no longer right; overwriting would restate a
    figure that may already be frozen under an approval (R4).
    """
    existing = _committed_sheet(net=Decimal("9999"))
    session = commit_session(sheets=[existing])
    response = commit(client, session)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "9999" in detail and "14035" in detail
    assert session.added == []


def test_a_scale_difference_is_not_a_divergence(client) -> None:
    """`numeric(14,2)` round-trips 14035 as 14035.00. Same payout (R7)."""
    session = commit_session(sheets=[_committed_sheet(net=Decimal("14035.00"))])
    response = commit(client, session)

    assert response.status_code == 200
    assert response.json()["created"] is False


# =============================================================================
# R4 — what the commit opens
# =============================================================================


def test_a_commit_opens_version_one_in_draft(client) -> None:
    """Creation, not submission (R4).

    The version row is DRAFT, version 1, stamped with the committer — and there
    is exactly one, so the commit did not also transition it. Submission is a
    separate authenticated act against `app/api/approvals.py`, which writes its
    own audit row.
    """
    session = commit_session()
    body = commit(client, session).json()

    versions = session.versions()
    assert len(versions) == 1
    version = versions[0]
    assert version.artifact_type is ArtifactType.REMUNERATION_SHEET
    assert version.artifact_id == session.sheets()[0].id
    assert version.version == 1
    assert version.state is ArtifactState.DRAFT
    assert version.superseded_at is None
    assert body["artifact_state"] == ArtifactState.DRAFT.value


def test_a_commit_does_not_release_or_pay(client) -> None:
    """R3: committing is not releasing. Nothing here marks money as gone out."""
    session = commit_session()
    commit(client, session)

    sheet = session.sheets()[0]
    assert sheet.paid_on is None
    assert sheet.payout_status is DocStatus.IN_PROGRESS
    assert session.versions()[0].released_at is None


def test_the_sheet_the_version_and_the_audit_row_share_one_commit(client, audit) -> None:
    """§11 — one transaction, or none of it.

    `TransactionalAudit.write()` raises, so reaching this assertion at all proves
    `write_within()` was used; the single `commit()` proves the three writes were
    not landed separately.
    """
    session = commit_session()
    commit(client, session)

    assert session.commits == 1
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == payouts.PayoutAuditAction.COMMITTED.value
    assert event.entity_table == ArtifactType.REMUNERATION_SHEET.value
    assert event.entity_id == session.sheets()[0].id
    assert event.before is None
    assert event.after is not None
    assert event.after["net"] == "14035"
    assert event.after["invoice_number"] == "VEMA/26-27/JUL1"


# =============================================================================
# R2 / R7 — the persisted figures
# =============================================================================


def test_fixture_one_reconciles_to_the_rupee_through_the_commit(client) -> None:
    """CLAUDE.md §6: VEMA PRUDHVI SAI, bCAP 80,000/mo, 26-31 Jul 2026, TA&DA 100.

    Earned 15,484 · Gross 15,584 · TDS 1,548 · **Net 14,035**, asserted on the
    ROW rather than on the response — a response that is right about a row that
    is wrong pays the wrong amount.

    The intermediates are stored at FULL precision, unrounded, and are compared
    here at two places for that reason: R6 rounds once, at net, and a router that
    quantized `earned` on the way into the column would be doing money arithmetic
    outside the engine (R2). `numeric(14,2)` is where the two-place storage
    happens, and `net_amount` — the one figure §6 rounds — is exact.
    """
    session = commit_session()
    assert commit(client, session).status_code == 201

    sheet = session.sheets()[0]
    assert round(sheet.earned, 2) == Decimal("15483.87")
    assert round(sheet.gross, 2) == Decimal("15583.87")
    assert round(sheet.tds, 2) == Decimal("1548.39")
    assert sheet.net_amount == Decimal("14035")
    assert sheet.tds_rate == Decimal("0.10")
    assert sheet.days_in_month == 31
    assert sheet.rate == Decimal("80000")
    assert sheet.rate_basis is RateBasis.PER_MONTH
    assert sheet.currency == "INR"
    assert "Fourteen Thousand" in (sheet.amount_in_words or "")


def test_fixture_two_reconciles_to_the_rupee_through_the_commit(client) -> None:
    """CLAUDE.md §6: Bushily Kondala Rao, bCAP 65,000/mo, full July 2026.

    Earned 65,000 · TDS 6,500 · **Net 58,500**. The full-month path is the one
    where dividing before multiplying leaves 64,999.99999… — if that ever reaches
    the database this fails.
    """
    start, end = dt.date(2026, 7, 1), dt.date(2026, 7, 31)
    session = commit_session(
        marks=_present(start, end),
        work_order=_work_order(rate=Decimal("65000")),
        trainer_pan="BCDPK1234R",
        bank=_bank_account(),
    )
    response = commit(
        client,
        session,
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        ta_da="0",
    )

    assert response.status_code == 201, response.json()
    sheet = session.sheets()[0]
    assert sheet.earned == Decimal("65000")
    assert sheet.tds == Decimal("6500")
    assert sheet.net_amount == Decimal("58500")
    assert response.json()["invoice_number"] == "BCDP/26-27/JUL1"


def test_a_client_supplied_net_is_refused_outright(client) -> None:
    """R1/R2: a caller says WHICH payout, never what it comes to."""
    session = commit_session()
    assert commit(client, session, net="1").status_code == 422
    assert commit(client, session, earned="1").status_code == 422
    assert session.added == []


def test_a_json_float_never_reaches_a_money_column(client) -> None:
    """R7 at the boundary: `100.5` is a 422, `"100.5"` is accepted."""
    session = commit_session()
    assert commit(client, session, ta_da=100.5).status_code == 422
    assert session.added == []


def test_every_persisted_amount_is_a_decimal(client) -> None:
    """R7, on the row itself — a float in a money column is the defect."""
    session = commit_session()
    commit(client, session)

    sheet = session.sheets()[0]
    for column in (
        "rate",
        "payable_days",
        "earned",
        "ta_da",
        "accommodation",
        "travel_reimb",
        "gross",
        "tds_rate",
        "tds",
        "deductions",
        "net_amount",
    ):
        assert isinstance(getattr(sheet, column), Decimal), column


def test_the_work_order_is_snapshotted_onto_the_row(client) -> None:
    """`work_order_id` is the order the rate came from — the §7 audit trail."""
    order = _work_order()
    session = commit_session(work_order=order)
    commit(client, session)

    assert session.sheets()[0].work_order_id == order.id


def _present(start: dt.date, end: dt.date) -> dict[dt.date, AttendanceMark]:
    days = (end - start).days + 1
    return {start + dt.timedelta(days=n): AttendanceMark.PRESENT for n in range(days)}
