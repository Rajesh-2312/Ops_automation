"""Payout endpoints — compute, validate, and generate the two Finance sheets.

    GET  /payouts?month=YYYY-MM
    POST /payouts/preview
    POST /payouts/validate
    POST /payouts/commit
    POST /payouts/remuneration-sheet.xlsx
    POST /payouts/invoice-sheet.xlsx

This module is the HTTP surface of three already-tested service modules and it
adds nothing to them. `engine.py` owns the arithmetic (R2), `validators.py` owns
the §7 gates, `generators.py` owns the legacy column orders. What lives here is
I/O, authorisation and serialisation.

R2 — WHY THERE IS NO ARITHMETIC IN THIS FILE
============================================
"All monetary arithmetic lives in `services/remuneration/engine.py`." There is no
arithmetic operator applied to a monetary value anywhere below. Every rupee in a
response was read off a `PayoutResult` that `compute_payout()` produced, and the
one integer expression in the module (`_next_invoice_seq`) operates on an invoice
sequence number, which is not money. If you find yourself needing a `+` on an
amount, the value belongs in `PayoutResult`, not in a handler.

R7 — WHY MONEY IS A STRING ON THE WIRE
======================================
Pydantic v2 will coerce a JSON `float` into a `Decimal` field without complaint,
which is precisely the R7 violation `app.domain.money.money()` exists to refuse.
`Money` below runs that same `money()` guard as a `BeforeValidator`, so
`{"ta_da": 100.50}` is a 422 and `{"ta_da": "100.50"}` is accepted. Outbound, every
amount is serialised with `str()` rather than left to the default encoder, so a
`Decimal` can never reach a client as a JSON float and lose paise on the way.
Both directions have a test.

R5 — THE COMMERCIALS WALL, IN CODE
==================================
A payout IS commercial data — it is a rate, an earned figure and a bank
instruction in one object. We are on a `BYPASSRLS` service-role connection, so
`0700_finance.sql`'s policies do not run for us and nothing in the database will
stop an LDE Executive reading a trainer's net pay. `app/core/security.py` is
explicit about this. Every handler here therefore reproduces the policy in code:

    internal caller   can_see_commercials() AND can_reach_college(program)
    everyone else     403

Both conjuncts on the internal path are load-bearing: `require_commercials()`
alone lets a Manager price another cluster's trainer, `require_college_reach()`
alone lets an LDE Executive read a rate. An LDE Executive and a College persona
get 403 from all four endpoints before a single row is read.

**There is no trainer path, and there was one until SEC-06.** `POST
/payouts/preview` used to admit a trainer login for their own deployment. §4
settled it the other way on 2026-08-18 — "Trainers are records, not users" — and
migration 1800 dropped all eighteen trainer policies, so the database has had no
trainer door since; this file held the last one, on the connection where code is
the only wall. A trainer login is now refused by `_require_payout_persona()`
before any row is read, exactly like an LDE Executive. See that function for the
full argument, and §4 for what an educator portal would have to be instead.

R4 — NOTHING RELEASES FROM HERE
===============================
There is no release endpoint, no `mark_paid`, and no transition past DRAFT. Every
artifact this module produces is a `DRAFT` and says so, in the response body and
in the `X-Artifact-State` header. The DRAFT -> PENDING_APPROVAL -> APPROVED ->
RELEASED machine lives in `app/services/approval/` and its HTTP surface is
`app/api/approvals.py`; `POST /payouts/validate` reports whether a cycle *could*
leave DRAFT, and performs no transition itself.

`POST /payouts/commit` is the one write in this file. It is where a computed
payout becomes a `remuneration_sheets` row, gets its invoice number, and ENTERS
the lifecycle at version 1 in DRAFT — creation, not submission and not approval.
Its docstring argues that choice out. Everything else here still writes nothing.

R1 — WHAT COMES FROM THE DATABASE AND WHAT COMES FROM THE CALLER
================================================================
Attendance is queried, expanded day by day and counted by `app.domain.attendance`
— a payable-day count is never taken from a request. So are the rate and its
basis (the signed work order), the trainer's PAN and ZOHO account, the issued
invoice numbers and the trailing net pays behind the deviation warning.

Reimbursements, deductions and the TDS rate are supplied by the caller, because
they are claims rather than facts: today they arrive on a Google Form (§15) and
no system of record holds them. `rate`/`rate_basis` may also be supplied, and when
they are they are treated as an assertion to be checked — the §7
`rate_mismatch_with_work_order` gate compares them against the signed WO — never
as a substitute for it.

WHERE THE BANK RAILS COME FROM
==============================
`trainer_bank_accounts` (1400), read by `_payout_context()` — never the request
body. Payment rails supplied by a caller would be payment rails outside any
system of record, which R1 forbids, and `PayoutRequest` is `extra="forbid"` so
sending them is a 422 rather than a silent acceptance.

The table is separate from `trainers` because RLS is row-level and 0400 gives an
LDE Executive the trainer row; the rails sit behind `can_see_commercials()`. We
are on a BYPASSRLS connection, so that policy does not run for us — the wall is
reproduced in code by `_require_payout_persona()`, which has already refused an
LDE Executive before this row is read.

THE WORK QUEUE, AND WHY IT IS A LIST AND NOT A CALCULATOR
=========================================================
`GET /payouts?month=YYYY-MM` answers the question the four POST endpoints
cannot: *which* trainer-months are due. Without it a Manager must already know a
deployment id, which means picking one by hand out of every deployment they can
reach and remembering which ones they have done. That is how a month gets
missed, and a missed month is a trainer who is not paid.

It is triage, not computation. Each row carries who, where, which period, how
complete the attendance is, whether a signed work order covers the period, and
whether a payout already exists and in what state — enough to decide what to
open next and nothing more. There is deliberately no net pay for a payout that
has not been computed yet: producing one would mean running the engine over
every deployment in reach on a list request, and a figure obtained that cheaply
would be read as final. `POST /payouts/preview` is where a number comes from.

The one money field, `payout.net`, is read back off a persisted
`remuneration_sheets` row — a figure the engine already produced (R2) — and is
serialised as a string like every other amount here (R7). No arithmetic is
applied to it, not even a total: §6's chain lives in the engine and a sum
computed in a router is a second, untested implementation of money.

A trainer with no rails on file still previews and still generates: §7's
`bank_account_missing` and `ifsc_invalid` gates report BLOCKING and both sheets
write those cells empty, which `generators.py` wants — a blank rail "means the
caller is previewing something that cannot be released, and it should look
obviously incomplete rather than plausible".
"""

from __future__ import annotations

import calendar
import datetime as dt
import io
import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.audit import AuditEvent, AuditWriter, JsonValue, get_audit_writer
from app.core.security import (
    CurrentPrincipal,
    Principal,
    require_college_reach,
    require_commercials,
)
from app.db.models import (
    ArtifactVersion,
    Batch,
    College,
    Deployment,
    Profile,
    Program,
    RemunerationSheet,
    Trainer,
    TrainerAttendance,
    TrainerBankAccount,
    WorkOrder,
)
from app.db.session import get_session
from app.domain.attendance import PayableDays, expand_period, payable_days
from app.domain.enums import (
    ArtifactState,
    ArtifactType,
    AttendanceMark,
    DocStatus,
    Persona,
    ProgramType,
    RateBasis,
    ValidationCode,
    ValidationSeverity,
)
from app.domain.money import DEFAULT_TDS_RATE, ZERO, amount_in_words, money
from app.domain.payout import PayoutInput, PayoutResult
from app.services.remuneration.engine import compute_payout
from app.services.remuneration.generators import (
    PayoutRecord,
    TrainerIdentity,
    write_invoice_sheet,
    write_remuneration_sheet,
)
from app.services.remuneration.invoice_no import (
    InvoiceNumber,
    build_invoice_number,
    fiscal_year,
    month_token,
)
from app.services.remuneration.validators import (
    PayoutContext,
    ValidationIssue,
    ValidationReport,
    validate_payout,
)

router = APIRouter(prefix="/payouts", tags=["payouts"])

Session = Annotated[AsyncSession, Depends(get_session)]
Audit = Annotated[AuditWriter, Depends(get_audit_writer)]

#: The MIME type Excel registers for `.xlsx`.
XLSX_MEDIA_TYPE: Final[str] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Every artifact this module emits is a draft (R4). Sent as a header so a client
#: cannot render a downloaded sheet as though it were approved.
ARTIFACT_STATE_HEADER: Final[str] = "X-Artifact-State"

#: Whether the §7 gates blocked the payout the downloaded sheet describes, and
#: which gates did. A sheet is still generated — see `generate_remuneration_sheet`.
BLOCKED_HEADER: Final[str] = "X-Validation-Blocked"
BLOCKING_CODES_HEADER: Final[str] = "X-Validation-Blocking-Codes"

_NON_FILENAME = re.compile(r"[^A-Za-z0-9]+")


# --- R7 at the API boundary ---------------------------------------------------


def _decimal_only(value: object) -> Decimal:
    """Reject a `float` before Pydantic can quietly widen it into a `Decimal`.

    Delegates to `app.domain.money.money()` so "what counts as money" has exactly
    one definition in the codebase. `money()` raises `TypeError`, which Pydantic
    does not treat as a validation failure, so it is re-raised as `ValueError` and
    surfaces as a 422 naming the offending field instead of a 500.
    """
    try:
        return money(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(str(exc)) from exc


#: An amount on the wire. Inbound: `Decimal`, `int` or a decimal string — a JSON
#: float is refused (R7). Outbound: `str`, so no amount is ever rendered as a JSON
#: float. `payable_days` uses this too; a half day is 0.5 and the same rule applies.
Money = Annotated[
    Decimal,
    BeforeValidator(_decimal_only),
    PlainSerializer(str, return_type=str, when_used="json"),
]


class RateSource(StrEnum):
    """Where the rate the engine used came from.

    Lives here rather than in `app/domain/enums.py` for the same reason
    `app.core.audit.AuditAction` does: this vocabulary has one writer and one
    reader and is not stored in Postgres, so planting it in the domain layer from
    a module that does not own that file would be speculative. Move it there when
    a second workstream needs it.
    """

    WORK_ORDER = "work_order"
    REQUEST_OVERRIDE = "request_override"


class PayoutAuditAction(StrEnum):
    """Audit vocabulary for this router.

    `AuditEvent.action` is typed `str` precisely so a workstream can record its
    own actions without editing `app/core/audit.py` — see that module's docstring
    on `AuditAction`.
    """

    VALIDATED = "payout.validated"
    COMMITTED = "payout.committed"
    REMUNERATION_SHEET_GENERATED = "payout.remuneration_sheet_generated"
    INVOICE_SHEET_GENERATED = "payout.invoice_sheet_generated"


# --- requests -----------------------------------------------------------------


class PayoutRequest(BaseModel):
    """One trainer-month, identified by deployment.

    `extra="forbid"`: a mistyped `ta_da` must be a 422, not a silent zero. On a
    payout, a field that quietly defaults is a field that quietly underpays.

    The deployment is the identifier because it is the one row that names the
    trainer and the batch at once, which is also how `can_reach_deployment()`
    resolves scope in SQL.
    """

    model_config = ConfigDict(extra="forbid")

    deployment_id: UUID
    period_start: dt.date
    period_end: dt.date

    rate: Money | None = Field(
        default=None,
        description=(
            "Engagement rate asserted by the caller. Omit to use the signed work "
            "order's rate. Supplying one that disagrees with the WO does not "
            "override it — it trips the §7 rate_mismatch_with_work_order gate."
        ),
    )
    rate_basis: RateBasis | None = Field(
        default=None,
        description="Required with `rate`, never inferred from the program type.",
    )

    ta_da: Money = ZERO
    accommodation: Money = ZERO
    travel_reimbursement: Money = ZERO
    deductions: Money = ZERO

    tds_rate: Money | None = Field(
        default=None,
        description=f"Defaults to {DEFAULT_TDS_RATE} (§6). Override only for a "
        "lower-deduction certificate.",
    )

    @model_validator(mode="after")
    def _check_period(self) -> PayoutRequest:
        """One payout, one calendar month, and a rate that names its own basis.

        The single-month rule is not a convenience. `days_in_month` prorates a
        bCAP retainer, the remuneration sheet's eighth header is bound to the
        month, and the invoice number's fiscal year derives from it — a period
        straddling two months has no single correct answer for any of the three,
        and `write_remuneration_sheet()` rejects it downstream anyway.
        """
        if self.period_end < self.period_start:
            raise ValueError(f"period_end {self.period_end} precedes period_start")
        if (self.period_start.year, self.period_start.month) != (
            self.period_end.year,
            self.period_end.month,
        ):
            raise ValueError(
                f"period {self.period_start}..{self.period_end} spans two months; "
                "a payout period is one calendar month (§6)"
            )
        if (self.rate is None) != (self.rate_basis is None):
            raise ValueError(
                "rate and rate_basis must be supplied together — a rate without a "
                "basis would have to be guessed from the program type, and §5 puts "
                "the basis on the work order"
            )
        return self


class PayoutValidationRequest(PayoutRequest):
    """A payout plus the §7 reasons stated for its warnings."""

    stated_reasons: dict[ValidationCode, str] = Field(
        default_factory=dict,
        description=(
            "Reason per warning code. §7 permits a warned payout only with a "
            "stated reason; a blank string does not count."
        ),
    )


class PayoutCommitRequest(PayoutValidationRequest):
    """A payout to persist. Identity and claims only — never a figure.

    Inherits exactly the fields `POST /payouts/validate` takes, and deliberately
    adds none. R1/R2: what may be sent is *which* payout (deployment, period) and
    the claims no system of record holds (reimbursements, deductions, TDS rate);
    every rupee that lands in `remuneration_sheets` is recomputed here by
    `compute_payout()` from the marks, the rate and the PAN on file. There is no
    `net`, no `earned` and no `invoice_number` field, and `extra="forbid"` makes
    sending one a 422 rather than a silently ignored value.
    """


class SheetRequest(PayoutRequest):
    """A payout plus the invoice-sheet fields the engine never sees."""

    invoice_date: dt.date | None = Field(
        default=None,
        description=(
            "The invoice's own `date` cell. Left empty when omitted rather than "
            "defaulted to today: a regenerated invoice must reproduce the "
            "original date, and nothing is issued by this endpoint."
        ),
    )


# --- responses ----------------------------------------------------------------


class PayoutBreakdown(BaseModel):
    """The §6 chain, field for field off `PayoutResult`. Nothing is derived here.

    `net_unrounded` sits beside `net` deliberately: it is the evidence that R6's
    single rounding happened once, at the end, and a disputed rupee can be walked
    back through the response without rerunning anything.
    """

    model_config = ConfigDict(frozen=True)

    rate: Money
    rate_basis: RateBasis
    rate_source: RateSource
    payable_days: Money
    days_in_month: int

    rate_per_day: Money = Field(
        description=(
            "DISPLAY ONLY on the per-month path (R6). It is rate / days_in_month "
            "and it did not enter the calculation of `earned`; multiplying it by "
            "`payable_days` will not reproduce `earned`, and must not be tried."
        )
    )
    earned: Money
    reimbursements: Money
    gross: Money
    tds_rate: Money
    tds: Money = Field(description="Levied on `earned`, never on `gross` (§6).")
    deductions: Money
    net_unrounded: Money
    net: Money = Field(description="The only rounded figure in the chain (R6).")
    net_in_words: str

    @classmethod
    def of(cls, payout: PayoutInput, result: PayoutResult, source: RateSource) -> PayoutBreakdown:
        """Copy an engine result onto the wire. Pass-through, never re-derivation."""
        return cls(
            rate=payout.rate,
            rate_basis=result.rate_basis,
            rate_source=source,
            payable_days=result.payable_days,
            days_in_month=result.days_in_month,
            rate_per_day=result.rate_per_day,
            earned=result.earned,
            reimbursements=result.reimbursements,
            gross=result.gross,
            tds_rate=result.tds_rate,
            tds=result.tds,
            deductions=result.deductions,
            net_unrounded=result.net_unrounded,
            net=result.net,
            net_in_words=amount_in_words(result.net),
        )


class AttendanceSummary(BaseModel):
    """What the marks said, and how complete they were.

    `unmarked` is reported next to the count because §5's asymmetry makes the two
    inseparable: on bCAP an unmarked day was PAID, on CRT it was NOT, and a
    payable-day figure without its completeness is unreviewable.
    """

    model_config = ConfigDict(frozen=True)

    program_type: ProgramType
    payable_days: Money
    period_days: int
    marked: int
    unmarked: int
    is_complete: bool


class ExistingPayout(BaseModel):
    """A `remuneration_sheets` row that already covers this trainer-month.

    Its presence is the difference between "to do" and "done", and its `state` is
    the difference between "done" and "waiting on Finance". `net` is quoted as
    persisted — the engine wrote it (R2) — and stringified (R7).
    """

    model_config = ConfigDict(frozen=True)

    sheet_id: UUID
    period_start: dt.date
    period_end: dt.date
    payout_status: DocStatus
    net: Money | None = Field(
        description="Net pay as persisted by the engine. Null on a row not yet computed."
    )
    invoice_no: str | None
    paid_on: dt.date | None


class PayoutQueueItem(BaseModel):
    """One candidate trainer-month, with enough state to triage it.

    Everything here was read from a system of record. Nothing is computed except
    the payable-day count, which comes from `app.domain.attendance` over the marks
    on file — the same pure functions the preview uses, so the two cannot disagree
    about what the marks say.
    """

    model_config = ConfigDict(frozen=True)

    deployment_id: UUID
    trainer_id: UUID
    trainer_name: str
    trainer_pan: str

    college_id: UUID
    college_name: str
    program_id: UUID
    program_name: str
    batch_name: str

    period_start: dt.date = Field(
        description=(
            "The requested month clipped to the deployment's own window. A trainer "
            "who started on the 26th is due for the 26th to the 31st, and that is "
            "the period `POST /payouts/preview` should be called with."
        )
    )
    period_end: dt.date

    attendance: AttendanceSummary
    work_order_signed: bool = Field(
        description=(
            "A signed work order is on file covering this period (§7's first "
            "blocking gate). False also when one exists but expired mid-month — "
            "the distinction is `POST /payouts/validate`'s to report."
        )
    )
    payout: ExistingPayout | None = Field(
        description="The payout already on file for this period, or null if none."
    )


class PayoutQueue(BaseModel):
    """The month's candidates, ordered college, program, trainer.

    `month` echoes what was asked for and `period_start`/`period_end` bound it, so
    a client rendering the queue never has to re-derive the calendar month — and
    an item's own period, which may be shorter, is never mistaken for it.
    """

    model_config = ConfigDict(frozen=True)

    month: str
    period_start: dt.date
    period_end: dt.date
    count: int
    items: tuple[PayoutQueueItem, ...]


class PayoutPreview(BaseModel):
    """A computed, unapproved payout."""

    model_config = ConfigDict(frozen=True)

    deployment_id: UUID
    trainer_pan: str
    trainer_name: str
    college_name: str
    program_name: str
    period_start: dt.date
    period_end: dt.date
    payout_month: dt.date

    invoice_number: str | None = Field(
        description=(
            "Provisional. Generated from PAN, the payout month's fiscal year and "
            "the next free sequence; the database's unique constraint is what "
            "actually reserves it, and nothing is reserved by this call. Null when "
            "the trainer's PAN is malformed — the §7 pan_invalid gate says why."
        )
    )
    artifact_state: ArtifactState = Field(
        default=ArtifactState.DRAFT,
        description="Always DRAFT. This endpoint cannot approve or release (R4).",
    )

    attendance: AttendanceSummary
    breakdown: PayoutBreakdown


class ValidationIssueOut(BaseModel):
    """One failed §7 gate, with the operative figures that made it fail."""

    model_config = ConfigDict(frozen=True)

    code: ValidationCode
    severity: ValidationSeverity
    message: str
    detail: dict[str, str]


class ValidationReportOut(BaseModel):
    """The §7 outcome, blocking and warning kept apart.

    Returned with 200 even when blocked. A blocked payout is a successful answer
    to "may this be paid?" — turning it into a 4xx would leave a Manager with an
    error page instead of the list of things to fix.
    """

    model_config = ConfigDict(frozen=True)

    is_blocked: bool
    can_submit: bool = Field(
        description=(
            "Whether this payout may move DRAFT -> PENDING_APPROVAL: no blocking "
            "issue, and a non-blank stated reason for every warning. Reporting "
            "only — the transition itself belongs to app/services/approval/."
        )
    )
    blocking: tuple[ValidationIssueOut, ...]
    warnings: tuple[ValidationIssueOut, ...]
    reasons_required: tuple[ValidationCode, ...]
    reasons_missing: tuple[ValidationCode, ...]

    @classmethod
    def of(
        cls, report: ValidationReport, stated_reasons: dict[ValidationCode, str]
    ) -> ValidationReportOut:
        return cls(
            is_blocked=report.is_blocked,
            can_submit=report.can_submit(stated_reasons),
            blocking=tuple(_issue_out(i) for i in report.blocking),
            warnings=tuple(_issue_out(i) for i in report.warnings),
            reasons_required=report.requires_reason,
            reasons_missing=tuple(
                code for code in report.requires_reason if not stated_reasons.get(code, "").strip()
            ),
        )


class PayoutValidation(BaseModel):
    """The number and the verdict together.

    One response rather than two calls: a Manager deciding what to fix needs the
    net pay and the reason it is blocked side by side, and two round trips can
    disagree if attendance is marked in between.
    """

    model_config = ConfigDict(frozen=True)

    preview: PayoutPreview
    report: ValidationReportOut


class CommittedPayout(BaseModel):
    """A persisted payout: the sheet row, its invoice number and its R4 version.

    `preview` is the recomputation this request performed, so the figures a
    caller reads back are the figures that were written — on the idempotent
    replay too, where they are also proved equal to the stored row before the
    response is built.
    """

    model_config = ConfigDict(frozen=True)

    sheet_id: UUID
    created: bool = Field(
        description=(
            "True when this request wrote the row. False when it already existed "
            "and this call changed nothing — see `commit_payout`'s docstring."
        )
    )
    invoice_number: str = Field(description="Issued and reserved. Never supplied by the caller.")
    artifact_type: ArtifactType = ArtifactType.REMUNERATION_SHEET
    artifact_state: ArtifactState = Field(
        description="The state of the artifact's current version. DRAFT on creation (R4)."
    )
    artifact_version: int = Field(ge=1)

    preview: PayoutPreview
    report: ValidationReportOut


def _issue_out(issue: ValidationIssue) -> ValidationIssueOut:
    """Adapt a `validators.ValidationIssue` onto the wire."""
    return ValidationIssueOut(
        code=issue.code,
        severity=issue.severity,
        message=issue.message,
        detail=dict(issue.detail),
    )


# --- what one payout needs, once fetched --------------------------------------


@dataclass(frozen=True, slots=True)
class _Resolved:
    """Everything the engine, the gates and the generators need, already fetched.

    Assembled once per request by `_payout_context()` so the four handlers share a
    single authorisation path and a single set of queries. Nothing on it is
    computed; it is the structured input CLAUDE.md §3 says the caller must fetch.
    """

    deployment: Deployment
    program: Program
    college: College
    trainer: Trainer
    #: The trainer's payment rails, or `None` when none are on file — in which
    #: case §7 blocks and the sheets' bank cells write empty. Never invented.
    bank: TrainerBankAccount | None
    #: The order the rate came from, or the latest one on file when none covers
    #: the period. `POST /payouts/commit` snapshots its id onto the sheet.
    work_order: WorkOrder | None
    #: Every `remuneration_sheets` row already on file for this trainer, read in
    #: this transaction. The commit path reads the idempotency row and the next
    #: invoice sequence off it rather than issuing a second query that could see
    #: a different snapshot.
    sheets: tuple[RemunerationSheet, ...]
    payout: PayoutInput
    result: PayoutResult
    rate_source: RateSource
    attendance: PayableDays
    context: PayoutContext
    payout_month: dt.date
    invoice_number: str | None

    @property
    def preview(self) -> PayoutPreview:
        return PayoutPreview(
            deployment_id=self.deployment.id,
            trainer_pan=self.trainer.pan,
            trainer_name=self.trainer.full_name,
            college_name=self.college.name,
            program_name=self.program.name,
            period_start=self.context.period_start,
            period_end=self.context.period_end,
            payout_month=self.payout_month,
            invoice_number=self.invoice_number,
            attendance=AttendanceSummary(
                program_type=self.attendance.program_type,
                payable_days=self.attendance.days,
                period_days=self.attendance.period_days,
                marked=self.attendance.marked,
                unmarked=self.attendance.unmarked,
                is_complete=self.attendance.is_complete,
            ),
            breakdown=PayoutBreakdown.of(self.payout, self.result, self.rate_source),
        )

    def report(self, stated_reasons: dict[ValidationCode, str]) -> ValidationReportOut:
        return ValidationReportOut.of(validate_payout(self.context), stated_reasons)


# --- authorisation ------------------------------------------------------------


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _require_payout_persona(principal: Principal) -> None:
    """The wall, applied before any row is read. Senior Manager and Manager only.

    Persona first, exactly as `programs.py` does it: an LDE Executive or a College
    login is refused without the endpoint disclosing whether the deployment they
    named exists. `require_commercials()` is the same predicate the SQL policies
    use.

    SEC-06 — THE TRAINER CARVE-OUT IS GONE, AND MUST NOT COME BACK
    --------------------------------------------------------------
    This used to take a `trainer_may_read` flag, and `POST /payouts/preview`
    passed it `True` so a trainer login could read their own payout. CLAUDE.md §4
    settled the question the other way (owner's decision, 2026-08-18): **trainers
    are records, not users.** An educator never signs in; work orders,
    deployments, attendance and payouts are managed *for* them. Migration 1800
    dropped all eighteen trainer policies to match, so the database has had no
    trainer-shaped door since — this was the last one left, in code, on a
    BYPASSRLS connection where code is the only wall.

    It was not exploitable: reaching a row also needed `profiles.trainer_id`,
    which only an admin can set (`profiles_guard_privileged_columns`). That is an
    argument for it being low severity, not for keeping it. §4 also warns that
    the `trainer` label survives in `app_role` purely as the DENY-BY-DEFAULT
    sentinel for a malformed signup — a live code path that hands that sentinel a
    commercial figure is the sentinel doing the opposite of its job.

    If an educator portal is ever wanted, §4 says it is a new build against the
    existing `trainers` / `deployments` tables. It is not this flag coming back.
    """
    require_commercials(principal)


async def _own_trainer_id(session: AsyncSession, principal: Principal) -> UUID | None:
    """The `trainers` row a trainer login is attached to, via `profiles.trainer_id`.

    Read per request from the database rather than carried on the token or the
    `Principal`, for the reason `app/core/security.py` gives: a JWT keeps
    asserting whatever it was minted with, long after the link is changed.
    """
    profile = (
        await session.execute(select(Profile).where(Profile.id == principal.user_id))
    ).scalar_one_or_none()
    return profile.trainer_id if profile is not None else None


async def _authorised_deployment(
    session: AsyncSession, principal: Principal, deployment_id: UUID
) -> tuple[Deployment, Program]:
    """Load the deployment and its program, or refuse.

    Two different failure shapes, on purpose:

    * An internal caller who has cleared the commercials wall gets 404 for a
      missing deployment and 403 for one outside their reach. They are already
      trusted with the existence of the estate they cover.
    * A trainer gets 403 either way. Distinguishing "no such deployment" from "not
      yours" would turn this endpoint into an id oracle, and a trainer has no
      business knowing the size of the deployment table.

    The trainer branch is now UNREACHABLE through any route in this module:
    `_payout_context()` calls `_require_payout_persona()` first and that refuses
    the persona outright since SEC-06. It is kept rather than deleted because it
    is a REFUSAL, not a grant — deleting it would leave this helper answering 404
    to a trainer if it were ever called from somewhere that had not already
    closed the wall, and a fail-closed branch that costs nothing is worth more
    than a tidier function. Do not read it as the carve-out surviving.
    """
    deployment = await session.get(Deployment, deployment_id)

    if principal.persona is Persona.TRAINER:
        own = await _own_trainer_id(session, principal)
        if deployment is None or own is None or deployment.trainer_id != own:
            raise _forbidden("You do not have access to this payout")
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")

    batch = await session.get(Batch, deployment.batch_id)
    program = await session.get(Program, batch.program_id) if batch is not None else None
    if batch is None or program is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment is not attached to a program",
        )

    if principal.persona is not Persona.TRAINER:
        # Both conjuncts, the way every policy in 0700_finance.sql is written.
        # The wall alone would let a Manager price another cluster's trainer; the
        # scope alone would let an LDE Executive read a rate.
        require_commercials(principal)
        require_college_reach(principal, program.college_id)

    return deployment, program


# --- fetching -----------------------------------------------------------------


async def _bank_account(session: AsyncSession, trainer: Trainer) -> TrainerBankAccount | None:
    """The trainer's payment rails from `trainer_bank_accounts` (1400), or `None`.

    R1: the database owns truth. A missing row is returned as `None` rather than
    raising, because "no rails on file" is a §7 verdict (`bank_account_missing`,
    `ifsc_invalid`) and not an HTTP error — the caller asked what this payout
    looks like, and the useful answer is the full breakdown plus the two gates
    naming exactly what Finance still needs.

    Selected rather than `session.get()` so the query is expressed against the
    mapped column; the row is 1:1 with the trainer, so at most one comes back.
    """
    return (
        await session.execute(
            select(TrainerBankAccount).where(TrainerBankAccount.trainer_id == trainer.id)
        )
    ).scalar_one_or_none()


async def _attendance(
    session: AsyncSession, deployment: Deployment, program: Program, request: PayoutRequest
) -> PayableDays:
    """Count payable days from the marks on file. R1: never from the request.

    Sparse rows are expanded to every calendar day of the period first. Skipping
    that would shorten the period itself on the bCAP path, where the count starts
    at the period length, and silently underpay a retainer.
    """
    rows = (
        (
            await session.execute(
                select(TrainerAttendance).where(
                    TrainerAttendance.deployment_id == deployment.id,
                    TrainerAttendance.mark_date >= request.period_start,
                    TrainerAttendance.mark_date <= request.period_end,
                )
            )
        )
        .scalars()
        .all()
    )

    # Collapsing duplicates into a dict would hide a double-marked day, and a
    # duplicated `A` deducts twice on the bCAP path. §6 is one row per day, so a
    # second row for the same day is a data error and is reported as one rather
    # than silently resolved by whichever row the dict comprehension saw last.
    repeated = sorted(str(day) for day, n in Counter(r.mark_date for r in rows).items() if n > 1)
    if repeated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Duplicate attendance rows for {', '.join(repeated)} — one row per day (§6)",
        )

    marks = {row.mark_date: row.mark for row in rows}
    return payable_days(
        program.type, expand_period(request.period_start, request.period_end, marks)
    )


async def _work_order(
    session: AsyncSession, deployment: Deployment, program: Program, request: PayoutRequest
) -> WorkOrder | None:
    """The work order governing this period, or the latest one if none covers it.

    Returning the latest rather than nothing when no WO covers the period is what
    lets §7's `work_order_period_mismatch` gate fire and name the window that
    actually was signed. Returning `None` would collapse "expired mid-month" into
    "no work order at all" and send whoever is fixing it to the wrong place.
    """
    orders = (
        (
            await session.execute(
                select(WorkOrder).where(
                    WorkOrder.trainer_id == deployment.trainer_id,
                    WorkOrder.program_id == program.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not orders:
        return None
    covering = [
        order
        for order in orders
        if order.valid_from <= request.period_start and request.period_end <= order.valid_to
    ]
    if covering:
        return min(covering, key=lambda order: order.valid_from)
    return max(orders, key=lambda order: order.valid_from)


def _next_invoice_seq(sheets: Sequence[RemunerationSheet], fy: str, month: str) -> int:
    """Next free sequence for this trainer, fiscal year and month.

    An ordinal, not an amount — the only integer expression in this module.
    §6 warns that scanning for a free sequence in Python races under concurrent
    runs; the database's `(pan, fiscal_year, month, seq)` unique index is the real
    arbiter, and this call reserves nothing.
    """
    used = [
        sheet.invoice_seq
        for sheet in sheets
        if sheet.invoice_seq is not None and sheet.invoice_fy == fy and sheet.invoice_month == month
    ]
    return max(used, default=0) + 1


async def _payout_context(
    session: AsyncSession, principal: Principal, request: PayoutRequest
) -> _Resolved:
    """Authorise, fetch, compute. The single path all four endpoints take.

    No per-endpoint persona knob any more. `trainer_may_read` was the SEC-06
    carve-out's only caller and every endpoint here now takes the identical wall —
    see `_require_payout_persona()`.
    """
    _require_payout_persona(principal)
    deployment, program = await _authorised_deployment(session, principal, request.deployment_id)

    college = await session.get(College, program.college_id)
    trainer = await session.get(Trainer, deployment.trainer_id)
    if college is None or trainer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment is missing its trainer or college record",
        )

    bank = await _bank_account(session, trainer)
    attendance = await _attendance(session, deployment, program, request)
    work_order = await _work_order(session, deployment, program, request)

    rate, basis, rate_source = _resolve_rate(request, work_order)

    payout_month = request.period_start
    days_in_month = calendar.monthrange(payout_month.year, payout_month.month)[1]

    payout = PayoutInput(
        program_type=program.type,
        rate_basis=basis,
        rate=rate,
        payable_days=attendance.days,
        days_in_month=days_in_month,
        period_start=request.period_start,
        period_end=request.period_end,
        ta_da=request.ta_da,
        accommodation=request.accommodation,
        travel_reimbursement=request.travel_reimbursement,
        deductions=request.deductions,
        tds_rate=request.tds_rate if request.tds_rate is not None else DEFAULT_TDS_RATE,
    )
    result = compute_payout(payout)

    sheets = await _trainer_sheets(session, trainer)
    fy, month = fiscal_year(payout_month), month_token(payout_month)
    invoice_number = _invoice_number(
        trainer.pan, payout_month, _next_invoice_seq(sheets, fy, month)
    )

    context = PayoutContext(
        program_type=program.type,
        result=result,
        period_start=request.period_start,
        period_end=request.period_end,
        days_in_month=days_in_month,
        attendance_complete=attendance.is_complete,
        unmarked_days=attendance.unmarked,
        work_order_signed=work_order is not None and work_order.status is DocStatus.SIGNED,
        wo_valid_from=work_order.valid_from if work_order else None,
        wo_valid_to=work_order.valid_to if work_order else None,
        wo_rate=work_order.rate if work_order else None,
        engagement_rate=rate,
        zoho_account_id=trainer.zoho_id,
        pan=trainer.pan,
        # R1: off `trainer_bank_accounts`, never off the request. Absent rails
        # stay absent — §7 blocks and says so rather than the router inventing a
        # payment instruction.
        bank_account_number=bank.bank_account_number if bank else None,
        ifsc=bank.ifsc if bank else None,
        invoice_number=invoice_number,
        issued_invoice_numbers=frozenset(s.invoice_no for s in sheets if s.invoice_no),
        trailing_net_pays=_trailing_net_pays(sheets, request.period_start),
    )

    return _Resolved(
        deployment=deployment,
        program=program,
        college=college,
        trainer=trainer,
        bank=bank,
        work_order=work_order,
        sheets=sheets,
        payout=payout,
        result=result,
        rate_source=rate_source,
        attendance=attendance,
        context=context,
        payout_month=payout_month,
        invoice_number=invoice_number,
    )


def _resolve_rate(
    request: PayoutRequest, work_order: WorkOrder | None
) -> tuple[Decimal, RateBasis, RateSource]:
    """The rate the engine will use, and where it came from.

    The signed work order is the system of record and is used unless the caller
    explicitly asserts otherwise. An assertion does not win the argument — it is
    handed to §7's `rate_mismatch_with_work_order` gate as `engagement_rate` and
    blocks if it disagrees with the WO. With neither, there is no rate to compute
    from and the request is unprocessable; that is not a substitute for the
    `work_order_missing` gate, which still fires for an unsigned WO that does
    carry a rate.
    """
    if request.rate is not None and request.rate_basis is not None:
        return request.rate, request.rate_basis, RateSource.REQUEST_OVERRIDE
    if work_order is not None:
        return work_order.rate, work_order.rate_basis, RateSource.WORK_ORDER
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(
            "No work order on file for this trainer and program, and no rate "
            "supplied. A payout cannot be computed without a rate."
        ),
    )


async def _trainer_sheets(session: AsyncSession, trainer: Trainer) -> tuple[RemunerationSheet, ...]:
    """Every remuneration row already on file for this trainer, newest first."""
    rows = (
        (
            await session.execute(
                select(RemunerationSheet)
                .where(RemunerationSheet.trainer_id == trainer.id)
                .order_by(RemunerationSheet.period_start.desc())
            )
        )
        .scalars()
        .all()
    )
    return tuple(rows)


def _trailing_net_pays(
    sheets: Sequence[RemunerationSheet], period_start: dt.date
) -> tuple[Decimal, ...]:
    """Net pay for up to the three months before this one, most recent first.

    Strictly earlier periods only: including the row for the period being
    recomputed would compare a payout against itself and mute the §7 deviation
    warning exactly when a re-run changed the number.
    """
    return tuple(
        sheet.net_amount
        for sheet in sheets
        if sheet.period_start < period_start and sheet.net_amount is not None
    )[:3]


def _invoice_number(pan: str, payout_month: dt.date, seq: int) -> str | None:
    """The provisional invoice number, or `None` if the PAN cannot seed one.

    `build_invoice_number()` raises on a malformed PAN, and rightly so — a bad PAN
    produces a plausible number that collides with another trainer's. Here that
    must not become a 500: the caller asked what this payout looks like, and the
    honest answer is the full breakdown plus a §7 `pan_invalid` block explaining
    why there is no number yet.
    """
    try:
        return str(build_invoice_number(pan, payout_month, seq))
    except ValueError:
        return None


# --- the work queue -----------------------------------------------------------

#: `YYYY-MM`. Validated here rather than by a Pydantic pattern so the 422 can say
#: what a payout month is; §6 makes the month, not an arbitrary range, the unit.
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month_window(month: str) -> tuple[dt.date, dt.date]:
    """`"2026-07"` -> the first and last calendar day of that month.

    `calendar.monthrange` supplies the length, which is also `days_in_month` on
    the bCAP path (§6) — one source for "how long is this month", so the queue and
    the engine cannot disagree about July.
    """
    if not _MONTH_RE.match(month):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"month must be YYYY-MM, got {month!r}. A payout period is one "
                "calendar month (§6), so the queue is asked for one."
            ),
        )
    year, mon = int(month[:4]), int(month[5:7])
    return dt.date(year, mon, 1), dt.date(year, mon, calendar.monthrange(year, mon)[1])


def _clip(deployment: Deployment, start: dt.date, end: dt.date) -> tuple[dt.date, dt.date] | None:
    """The deployment's window intersected with the month, or `None` if disjoint.

    Done in Python rather than in the WHERE clause on purpose: `start_date` and
    `end_date` are both nullable, and an open-ended deployment must count as
    running for the whole month rather than dropping out of the queue because SQL
    compared against NULL. Expressing that once, here, also makes it the same
    expression that produces the period the caller then previews with — a filter
    and a clip that could drift apart would put a trainer in the queue for a
    period they were not deployed in.
    """
    lo = max(deployment.start_date or start, start)
    hi = min(deployment.end_date or end, end)
    return (lo, hi) if lo <= hi else None


async def _rows_in(
    session: AsyncSession,
    model: type[Any],
    column: InstrumentedAttribute[Any],
    ids: Sequence[Any],
) -> Sequence[Any]:
    """`SELECT * FROM model WHERE column IN ids`, skipping the round trip when empty."""
    if not ids:
        return ()
    return (await session.execute(select(model).where(column.in_(list(ids))))).scalars().all()


@router.get(
    "",
    response_model=PayoutQueue,
    status_code=status.HTTP_200_OK,
    summary="List the trainer-months due for payout in a given month",
)
async def list_payout_queue(
    principal: CurrentPrincipal,
    session: Session,
    month: Annotated[
        str,
        Query(
            description="Payout month as YYYY-MM. One calendar month (§6).",
            examples=["2026-07"],
        ),
    ],
) -> PayoutQueue:
    """Every deployment in the caller's reach that ran during `month`.

    Internal only, and the wall closes before a single row is read: a payout queue
    names trainers, their periods and their existing net pay, which is commercial
    data (§4). An LDE Executive, a College login and a trainer all get 403 with
    zero queries issued — R5, reproduced in code because the service-role
    connection carries BYPASSRLS and `0700_finance.sql`'s policies never run for
    us.

    Scope is `principal.college_ids`, the app-side value of `my_college_ids()`, so
    a Senior Manager sees their cluster and a Manager their colleges. A caller with
    no assignments reaches nothing and the queue is empty — deny by default, the
    same answer SQL would give.

    Pure read. Nothing is written, no invoice number is reserved, no state moves,
    so no audit row is raised; §11 wants audit rows for state transitions, and
    listing is not one.
    """
    _require_payout_persona(principal)
    period_start, period_end = _month_window(month)

    reach = sorted(principal.college_ids)
    if not reach:
        return PayoutQueue(
            month=month,
            period_start=period_start,
            period_end=period_end,
            count=0,
            items=(),
        )

    colleges = {c.id: c for c in await _rows_in(session, College, College.id, reach)}
    programs = {
        p.id: p for p in await _rows_in(session, Program, Program.college_id, list(colleges))
    }
    batches = {b.id: b for b in await _rows_in(session, Batch, Batch.program_id, list(programs))}
    deployments = [
        d for d in await _rows_in(session, Deployment, Deployment.batch_id, list(batches))
    ]

    # Clip first, then fetch the per-row detail for the survivors only. A college
    # with fifty deployments and three live ones should cost three deployments'
    # worth of attendance, not fifty.
    live: list[tuple[Deployment, dt.date, dt.date]] = []
    for deployment in deployments:
        window = _clip(deployment, period_start, period_end)
        if window is not None:
            live.append((deployment, *window))
    if not live:
        return PayoutQueue(
            month=month,
            period_start=period_start,
            period_end=period_end,
            count=0,
            items=(),
        )

    deployment_ids = [d.id for d, _, _ in live]
    trainer_ids = sorted({d.trainer_id for d, _, _ in live})

    trainers = {t.id: t for t in await _rows_in(session, Trainer, Trainer.id, trainer_ids)}
    marks = await _queue_marks(session, deployment_ids, period_start, period_end)
    orders = await _rows_in(session, WorkOrder, WorkOrder.trainer_id, trainer_ids)
    sheets = await _rows_in(session, RemunerationSheet, RemunerationSheet.trainer_id, trainer_ids)

    items: list[PayoutQueueItem] = []
    for deployment, start, end in live:
        batch = batches.get(deployment.batch_id)
        program = programs.get(batch.program_id) if batch else None
        college = colleges.get(program.college_id) if program else None
        trainer = trainers.get(deployment.trainer_id)
        if batch is None or program is None or college is None or trainer is None:
            # A deployment whose parents did not come back is a broken row, not a
            # payout candidate. Skipping it keeps a data defect out of the queue
            # rather than turning the whole page into a 500; `POST /payouts/preview`
            # reports it precisely when somebody asks about that deployment.
            continue

        counted = payable_days(
            program.type, expand_period(start, end, marks.get(deployment.id, {}))
        )
        items.append(
            PayoutQueueItem(
                deployment_id=deployment.id,
                trainer_id=trainer.id,
                trainer_name=trainer.full_name,
                trainer_pan=trainer.pan,
                college_id=college.id,
                college_name=college.name,
                program_id=program.id,
                program_name=program.name,
                batch_name=batch.name,
                period_start=start,
                period_end=end,
                attendance=AttendanceSummary(
                    program_type=counted.program_type,
                    payable_days=counted.days,
                    period_days=counted.period_days,
                    marked=counted.marked,
                    unmarked=counted.unmarked,
                    is_complete=counted.is_complete,
                ),
                work_order_signed=_covering_order_signed(
                    orders, trainer.id, program.id, start, end
                ),
                payout=_existing_payout(sheets, trainer.id, program.id, start, end),
            )
        )

    items.sort(key=lambda i: (i.college_name, i.program_name, i.trainer_name, i.period_start))
    return PayoutQueue(
        month=month,
        period_start=period_start,
        period_end=period_end,
        count=len(items),
        items=tuple(items),
    )


async def _queue_marks(
    session: AsyncSession,
    deployment_ids: Sequence[UUID],
    period_start: dt.date,
    period_end: dt.date,
) -> dict[UUID, dict[dt.date, AttendanceMark]]:
    """Attendance marks for the month, grouped by deployment.

    A duplicated day is collapsed here rather than refused. `_attendance()` raises
    409 on one because a payout must not be computed off an ambiguous day; a
    queue's job is to get the Manager to that deployment, and a page that 409s
    because one row is duplicated somewhere in a cluster shows them nothing at all.
    """
    rows = (
        (
            await session.execute(
                select(TrainerAttendance).where(
                    TrainerAttendance.deployment_id.in_(list(deployment_ids)),
                    TrainerAttendance.mark_date >= period_start,
                    TrainerAttendance.mark_date <= period_end,
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[UUID, dict[dt.date, AttendanceMark]] = {}
    for row in rows:
        grouped.setdefault(row.deployment_id, {})[row.mark_date] = row.mark
    return grouped


def _covering_order_signed(
    orders: Sequence[WorkOrder],
    trainer_id: UUID,
    program_id: UUID,
    start: dt.date,
    end: dt.date,
) -> bool:
    """§7's first gate, as a triage flag: is a SIGNED order covering this period?

    Deliberately one boolean. "Signed but expired on the 20th" and "never sent"
    are different problems with different owners, and telling them apart is
    `POST /payouts/validate`'s job — it has the whole §7 report and the window that
    actually was signed. A queue that tried to say it in a column would say it
    less precisely.
    """
    return any(
        order.trainer_id == trainer_id
        and order.program_id == program_id
        and order.status is DocStatus.SIGNED
        and order.valid_from <= start
        and end <= order.valid_to
        for order in orders
    )


def _existing_payout(
    sheets: Sequence[RemunerationSheet],
    trainer_id: UUID,
    program_id: UUID,
    start: dt.date,
    end: dt.date,
) -> ExistingPayout | None:
    """The persisted payout overlapping this period, most recent first.

    Overlap rather than exact equality: a sheet written for 01–31 July still
    covers a trainer whose deployment started on the 26th, and reporting "no
    payout yet" for it would invite a second one. `invoice_no` carries the unique
    constraint, so the duplicate would be caught — but at the point where somebody
    has already redone the work.
    """
    matching = [
        sheet
        for sheet in sheets
        if sheet.trainer_id == trainer_id
        and sheet.program_id == program_id
        and sheet.period_start <= end
        and start <= sheet.period_end
    ]
    if not matching:
        return None
    sheet = max(matching, key=lambda s: s.period_start)
    return ExistingPayout(
        sheet_id=sheet.id,
        period_start=sheet.period_start,
        period_end=sheet.period_end,
        payout_status=sheet.payout_status,
        net=sheet.net_amount,
        invoice_no=sheet.invoice_no,
        paid_on=sheet.paid_on,
    )


# --- endpoints ----------------------------------------------------------------


@router.post(
    "/preview",
    response_model=PayoutPreview,
    status_code=status.HTTP_200_OK,
    summary="Compute a trainer-month payout without persisting anything",
)
async def preview_payout(
    request: PayoutRequest,
    principal: CurrentPrincipal,
    session: Session,
) -> PayoutPreview:
    """Run the §6 chain over the marks on file and return every intermediate.

    Pure read: nothing is written, nothing is reserved, no state moves, so no
    audit row is raised. The three endpoints that produce a durable artifact or a
    gate verdict do write one.

    Manager or Senior Manager with reach, and nobody else. An LDE Executive gets
    403 — a payout is a commercial (§4) — and so does a trainer login: §4's
    owner's decision of 2026-08-18 makes trainers records rather than users, and
    migration 1800 dropped every trainer policy to match (SEC-06). This endpoint
    carried the last carve-out; `_require_payout_persona()` records why it went.
    """
    resolved = await _payout_context(session, principal, request)
    return resolved.preview


@router.post(
    "/validate",
    response_model=PayoutValidation,
    status_code=status.HTTP_200_OK,
    summary="Run the §7 gates and report blocking issues and warnings",
)
async def validate_payout_endpoint(
    request: PayoutValidationRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> PayoutValidation:
    """Report whether this payout may leave DRAFT, and why not.

    Always 200. A blocked payout is an answer, not an error, and it is returned in
    full — every gate runs, so a Manager gets the whole list once rather than one
    blocker per round trip.

    This performs no transition. `can_submit` says the cycle *could* move to
    PENDING_APPROVAL; moving it is `app/services/approval/`'s to do, under a human
    session (R4).

    Internal only. The report exposes the work-order rate, the trailing net-pay
    average and the internal control logic behind them, none of which is a
    trainer's to read.
    """
    resolved = await _payout_context(session, principal, request)
    report = resolved.report(request.stated_reasons)

    await audit.write(
        AuditEvent(
            actor_id=principal.user_id,
            actor_persona=principal.persona,
            action=PayoutAuditAction.VALIDATED,
            entity_table=ArtifactType.REMUNERATION_SHEET.value,
            # No row yet — this payout has never been persisted. The deployment
            # and period in `after` are what identify it until it is.
            entity_id=None,
            before=None,
            after=_verdict(resolved, report),
        )
    )
    return PayoutValidation(preview=resolved.preview, report=report)


@router.post(
    "/commit",
    response_model=CommittedPayout,
    status_code=status.HTTP_201_CREATED,
    summary="Persist a computed payout, issue its invoice number and open its R4 version",
)
async def commit_payout(
    request: PayoutCommitRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
    response: Response,
) -> CommittedPayout:
    """Recompute, gate, issue, persist — the one place a payout becomes a row.

    Everything before this endpoint is arithmetic that evaporates. Without it
    `remuneration_sheets` stays empty, no invoice number is ever issued, and R4
    has no artifact to approve — a payout cannot be completed. Four things happen
    here and nothing else does.

    **1. The figures are recomputed, never accepted.** `_payout_context()` reads
    the marks, the signed work order's rate, the PAN and the bank rails from the
    database and runs `compute_payout()` (R1, R2). `PayoutCommitRequest` carries
    no monetary result at all, so there is no path by which a client-supplied
    number reaches a column.

    **2. The §7 gates run again and refuse.** Not a re-report of what
    `POST /payouts/validate` said a minute ago — the marks may have changed since,
    and the verdict that matters is the one taken in the same transaction as the
    write. `ValidationReport.can_submit(stated_reasons)` is the single predicate:
    it is false with any blocking issue, and false for a warning whose reason is
    missing or blank. Either way this is **409** and NOTHING is written — no row,
    no version, no invoice number, no audit row.

    **3. The invoice number is generated.** `build_invoice_number()` from the PAN
    and the payout month's fiscal year, with `seq` chosen as `max(existing) + 1`
    over the rows read inside this transaction — `invoice_no.py` prescribes
    exactly that, so the `(invoice_pan, invoice_fy, invoice_month, invoice_seq)`
    unique index arbitrates a concurrent race rather than this Python. A number is
    never taken from the caller: roughly one legacy number in five is malformed.
    `invoice_pan` is written as a SNAPSHOT of the PAN at issue and not left to a
    join, because 0700 freezes an issued number — a later PAN correction must not
    restate a number that has already been printed.

    **4. The R4 lifecycle opens, in DRAFT.**

    WHY DRAFT AND NOT PENDING_APPROVAL
    ==================================
    A committed sheet passes `can_submit()`, so it *could* be submitted here. It
    is not, for three reasons. R4 makes each transition a separate act with its
    own audit row, and auto-submitting would collapse creation and submission
    into one row that says a Senior Manager was asked when nobody asked. §8 puts
    "propose, human edits and sends" at autonomy level 2 and payouts explicitly no
    higher — the human who commits must be able to look at the row, download the
    two sheets against it and *then* push it. And committing is not yet
    irreversible: a DRAFT can be amended, whereas a PENDING_APPROVAL sitting in a
    Senior Manager's queue has already consumed somebody's attention. The
    transition is one `POST /approvals/remuneration_sheets/{id}/submit` away and
    that endpoint transitions the very row this one created.

    IDEMPOTENCY — REPLAY RETURNS, DIVERGENCE REFUSES
    ================================================
    `(trainer_id, program_id, period_start, period_end)` is unique in 0700, so a
    second commit for the same trainer-period cannot become a second sheet. The
    behaviour is chosen rather than delegated to an integrity error:

    * The row exists and the recomputation still lands on the same net pay — a
      retried request, a double-clicked button. **200**, the existing row, its
      existing invoice number, `created=false`. No second number is issued and
      no column is touched.
    * The row exists and the recomputation DISAGREES with it — attendance was
      marked, a rate was corrected. **409**, naming both figures. Silently
      returning the stale row would answer "committed" for a payout whose number
      has changed, and silently overwriting would restate a figure that may
      already be frozen under an approval. Restating a committed payout is a new
      version under R4 and belongs to an amend endpoint that does not exist yet.

    R3 — this commits, it does not release. There is no send here, no state past
    DRAFT, and no `paid_on`. R5 — Manager or Senior Manager only: an LDE Executive
    and a College login are refused by the wall before any row is read, and a
    trainer is refused too, because a trainer committing their own payout is the
    payee authorising their own payment.

    §11 — the sheet, the version row and the audit row go through
    `audit.write_within()` and one `commit()`. This is money; an unattributable
    payout row is worse than no payout row.
    """
    resolved = await _payout_context(session, principal, request)
    report = resolved.report(request.stated_reasons)

    existing = _committed_sheet(resolved)
    if existing is not None:
        return await _replay(session, resolved, report, existing, response)

    if not report.can_submit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This payout cannot be committed (§7). Blocking: "
                f"{', '.join(i.code.value for i in report.blocking) or 'none'}. "
                "Warnings without a stated reason: "
                f"{', '.join(c.value for c in report.reasons_missing) or 'none'}. "
                "POST /payouts/validate for the full report."
            ),
        )
    if resolved.invoice_number is None:  # pragma: no cover - the pan_invalid gate blocks first
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No invoice number can be seeded from PAN {resolved.trainer.pan!r} (§6)",
        )

    now = dt.datetime.now(dt.UTC)
    invoice = _issue_invoice(resolved)
    sheet = _sheet_row(resolved, invoice, now)
    session.add(sheet)

    version = ArtifactVersion(
        id=uuid.uuid4(),
        artifact_type=ArtifactType.REMUNERATION_SHEET,
        artifact_id=sheet.id,
        version=1,
        state=ArtifactState.DRAFT,
        created_by=principal.user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(version)

    after = _verdict(resolved, report)
    after["sheet_id"] = str(sheet.id)
    await audit.write_within(
        session,
        AuditEvent(
            actor_id=principal.user_id,
            actor_persona=principal.persona,
            action=PayoutAuditAction.COMMITTED,
            entity_table=ArtifactType.REMUNERATION_SHEET.value,
            entity_id=sheet.id,
            before=None,
            after=after,
            at=now,
        ),
    )
    await session.commit()

    return CommittedPayout(
        sheet_id=sheet.id,
        created=True,
        invoice_number=str(invoice),
        artifact_state=ArtifactState.DRAFT,
        artifact_version=version.version,
        preview=resolved.preview,
        report=report,
    )


def _committed_sheet(resolved: _Resolved) -> RemunerationSheet | None:
    """The row already covering this exact trainer-period, or `None`.

    Matched on the four columns 0700's `remuneration_sheets_unique_period` index
    covers, and on nothing else. An overlap match would be wrong here: the queue
    uses one to stop a Manager redoing work, but the constraint this endpoint has
    to stay on the right side of is equality, and refusing an adjacent period the
    database would happily accept would strand a mid-month re-deployment.
    """
    return next(
        (
            sheet
            for sheet in resolved.sheets
            if sheet.trainer_id == resolved.deployment.trainer_id
            and sheet.program_id == resolved.program.id
            and sheet.period_start == resolved.context.period_start
            and sheet.period_end == resolved.context.period_end
        ),
        None,
    )


async def _replay(
    session: AsyncSession,
    resolved: _Resolved,
    report: ValidationReportOut,
    existing: RemunerationSheet,
    response: Response,
) -> CommittedPayout:
    """Answer a repeated commit without issuing a second invoice number.

    The stored net is compared against the one just recomputed before anything is
    returned. Equality is checked with `!=` on two `Decimal`s, so `14035` and
    `14035.00` are the same payout — the column is `numeric(14,2)` and a scale
    difference on a round trip is not a divergence (R7 keeps both sides Decimal,
    which is what makes the comparison meaningful at all).
    """
    if existing.net_amount is None or existing.net_amount != resolved.result.net:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A payout is already committed for this trainer and period "
                f"(sheet {existing.id}, invoice {existing.invoice_no}, net "
                f"{existing.net_amount}), and recomputing now gives "
                f"{resolved.result.net}. A committed payout is not restated in "
                "place — that is a new version under R4."
            ),
        )
    if existing.invoice_no is None:  # pragma: no cover - never written without one
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Sheet {existing.id} exists with no invoice number and cannot be replayed",
        )

    version = await _current_artifact_version(session, existing.id)
    response.status_code = status.HTTP_200_OK
    return CommittedPayout(
        sheet_id=existing.id,
        created=False,
        invoice_number=existing.invoice_no,
        artifact_state=version.state if version is not None else ArtifactState.DRAFT,
        artifact_version=version.version if version is not None else 1,
        preview=resolved.preview,
        report=report,
    )


async def _current_artifact_version(
    session: AsyncSession, sheet_id: UUID
) -> ArtifactVersion | None:
    """The live version row for one sheet — `superseded_at is null` (1300).

    Read rather than assumed, because a replayed commit must report where the
    artifact actually got to: a sheet committed yesterday and approved this
    morning is APPROVED, and answering DRAFT would invite somebody to submit it
    again.
    """
    rows = (
        (
            await session.execute(
                select(ArtifactVersion).where(
                    ArtifactVersion.artifact_type == ArtifactType.REMUNERATION_SHEET,
                    ArtifactVersion.artifact_id == sheet_id,
                    ArtifactVersion.superseded_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return rows[0] if rows else None


def _issue_invoice(resolved: _Resolved) -> InvoiceNumber:
    """Reserve this payout's invoice number, inside the committing transaction.

    `seq` is `max(existing) + 1` over the rows this transaction already read —
    `invoice_no.py` prescribes exactly that shape and is explicit that the Python
    scan does not settle a race: two concurrent runs will both pick the same
    sequence, and the loser hits `remuneration_sheets_invoice_identity_key` and
    rolls back. That is the intended outcome and it is why the sequence is chosen
    here and not before the transaction opened.

    Cannot raise in practice: `build_invoice_number()` refuses a malformed PAN,
    and §7's `pan_invalid` gate has already blocked the commit for one.
    """
    return build_invoice_number(
        resolved.trainer.pan,
        resolved.payout_month,
        _next_invoice_seq(
            resolved.sheets,
            fiscal_year(resolved.payout_month),
            month_token(resolved.payout_month),
        ),
    )


def _sheet_row(resolved: _Resolved, invoice: InvoiceNumber, now: dt.datetime) -> RemunerationSheet:
    """One `remuneration_sheets` row, every figure copied off `PayoutResult`.

    A pure transcription — there is not an arithmetic operator in it (R2), and
    every amount is the `Decimal` the engine produced rather than a re-render of
    it (R7). Intermediates go in UNROUNDED — R6 rounds once, at `net`, and
    quantizing `earned` here would be money arithmetic performed outside the
    engine; `numeric(14,2)` is where two-place storage happens. `payout_status`
    opens at IN_PROGRESS: the payout exists and is being worked, and `paid_on`
    stays NULL because nothing here pays anybody (R3).

    The invoice identity is written as four components plus the assembled string
    because 0700 indexes both — the components carry §6's uniqueness claim and
    the string carries the "not already issued" gate.
    """
    result = resolved.result
    return RemunerationSheet(
        id=uuid.uuid4(),
        trainer_id=resolved.deployment.trainer_id,
        program_id=resolved.program.id,
        work_order_id=resolved.work_order.id if resolved.work_order is not None else None,
        period_start=resolved.context.period_start,
        period_end=resolved.context.period_end,
        rate=resolved.payout.rate,
        rate_basis=result.rate_basis,
        payable_days=result.payable_days,
        days_in_month=result.days_in_month,
        earned=result.earned,
        ta_da=resolved.payout.ta_da,
        accommodation=resolved.payout.accommodation,
        travel_reimb=resolved.payout.travel_reimbursement,
        gross=result.gross,
        tds_rate=result.tds_rate,
        tds=result.tds,
        deductions=result.deductions,
        net_amount=result.net,
        amount_in_words=amount_in_words(result.net),
        currency="INR",
        payout_status=DocStatus.IN_PROGRESS,
        paid_on=None,
        invoice_pan=resolved.trainer.pan.strip().upper(),
        invoice_fy=invoice.fiscal_year,
        invoice_month=invoice.month,
        invoice_seq=invoice.seq,
        invoice_no=str(invoice),
        invoice_issued_at=now,
        created_at=now,
        updated_at=now,
    )


@router.post(
    "/remuneration-sheet.xlsx",
    status_code=status.HTTP_200_OK,
    response_class=Response,
    summary="Download the 21-column remuneration sheet for one trainer-month",
)
async def generate_remuneration_sheet(
    request: SheetRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> Response:
    """Write the legacy 21-column sheet and return it as a DRAFT `.xlsx`.

    The column order is `generators.REMUNERATION_COLUMNS` and is an output
    contract (§11) — including the misspelled `Accomodation`, which Finance has
    VLOOKUPs written against.

    **A blocked payout still generates.** Refusing would strand the feature, since
    the bank-rail gates block every payout until the schema carries those columns
    (see the module docstring), and `generators.py` explicitly wants an
    un-releasable payout to produce a sheet that "looks obviously incomplete
    rather than plausible". What the caller gets instead of a refusal is the
    verdict in the response headers and an audit row naming every blocking gate.
    Approval, and therefore release, remains impossible until they are cleared.
    """
    resolved = await _payout_context(session, principal, request)
    record = _record(resolved, request)
    report = resolved.report({})

    buffer = io.BytesIO()
    write_remuneration_sheet([record], buffer, payout_month=resolved.payout_month)

    await audit.write(
        AuditEvent(
            actor_id=principal.user_id,
            actor_persona=principal.persona,
            action=PayoutAuditAction.REMUNERATION_SHEET_GENERATED,
            entity_table=ArtifactType.REMUNERATION_SHEET.value,
            entity_id=None,
            before=None,
            after=_verdict(resolved, report),
        )
    )
    return _xlsx(buffer, _filename("remuneration", resolved), report)


@router.post(
    "/invoice-sheet.xlsx",
    status_code=status.HTTP_200_OK,
    response_class=Response,
    summary="Download the 34-column invoice sheet for one trainer-month",
)
async def generate_invoice_sheet(
    request: SheetRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> Response:
    """Write the legacy 34-column invoice sheet and return it as a DRAFT `.xlsx`.

    `Amount in Words` is rendered in Python from `net`, so the words and the
    figure cannot disagree; the legacy sheet's `#NAME?` from a missing Excel macro
    is not reproduced (§6). `Expense for the month` and `AM Mail ID` are left
    empty — §14 Q2 and findings §1 are open questions, and an invented value would
    be indistinguishable from a real one within a month.
    """
    resolved = await _payout_context(session, principal, request)
    record = _record(resolved, request)
    report = resolved.report({})

    buffer = io.BytesIO()
    write_invoice_sheet([record], buffer)

    await audit.write(
        AuditEvent(
            actor_id=principal.user_id,
            actor_persona=principal.persona,
            action=PayoutAuditAction.INVOICE_SHEET_GENERATED,
            entity_table=ArtifactType.REMUNERATION_SHEET.value,
            entity_id=None,
            before=None,
            after=_verdict(resolved, report),
        )
    )
    return _xlsx(buffer, _filename("invoice", resolved), report)


# --- artifact assembly --------------------------------------------------------


def _record(resolved: _Resolved, request: SheetRequest) -> PayoutRecord:
    """Bind a computed payout to the identity and labels the sheets print.

    Bank rails come off `trainer_bank_accounts`; with no row on file every rail
    cell writes empty, which is what an un-releasable payout should look like.
    The invoice number is required, so a PAN that cannot seed
    one is refused with 409 rather than written as a blank into a document Finance
    would file — the §7 report is where that diagnosis belongs, and
    `POST /payouts/validate` returns it.
    """
    if resolved.invoice_number is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot issue a sheet: trainer PAN {resolved.trainer.pan!r} is malformed, "
                "so no invoice number can be seeded from it (§6). "
                "POST /payouts/validate for the full §7 report."
            ),
        )
    bank = resolved.bank
    return PayoutRecord(
        payout=resolved.payout,
        result=resolved.result,
        identity=TrainerIdentity(
            pan=resolved.trainer.pan,
            name=resolved.trainer.full_name,
            email=resolved.trainer.email,
            contact_number=resolved.trainer.phone,
            bank_account_number=bank.bank_account_number if bank else None,
            ifsc=bank.ifsc if bank else None,
            bank_name=bank.bank_name if bank else None,
            branch=bank.branch if bank else None,
            account_name=bank.account_name if bank else None,
        ),
        payout_month=resolved.payout_month,
        period_start=resolved.context.period_start,
        period_end=resolved.context.period_end,
        invoice_number=resolved.invoice_number,
        college_name=resolved.college.name,
        program_name=resolved.program.name,
        invoice_date=request.invoice_date,
    )


def _verdict(resolved: _Resolved, report: ValidationReportOut) -> dict[str, JsonValue]:
    """The audit `after` snapshot: what was produced, and whether §7 allowed it.

    Amounts are stringified, never floated — an audit row that rounds the figure
    it is attesting to is worse than no audit row (R7). Counts, not row dumps, for
    the same reason `programs.py` gives.
    """
    return {
        "deployment_id": str(resolved.deployment.id),
        "trainer_pan": resolved.trainer.pan,
        "period_start": resolved.context.period_start.isoformat(),
        "period_end": resolved.context.period_end.isoformat(),
        "invoice_number": resolved.invoice_number,
        "payable_days": str(resolved.result.payable_days),
        "net": str(resolved.result.net),
        "artifact_state": ArtifactState.DRAFT.value,
        "is_blocked": report.is_blocked,
        "can_submit": report.can_submit,
        "blocking_codes": [issue.code.value for issue in report.blocking],
        "warning_codes": [issue.code.value for issue in report.warnings],
        "reasons_missing": [code.value for code in report.reasons_missing],
    }


def _filename(kind: str, resolved: _Resolved) -> str:
    """`[kind]_[College]_[YYYY-MM]_DRAFT.xlsx`, in the §15 naming spirit.

    ASCII-only and punctuation-free: a college name with a comma or a non-Latin
    character would need RFC 5987 encoding in `Content-Disposition`, and a
    filename is not worth that.
    """
    college = _NON_FILENAME.sub("_", resolved.college.name).strip("_") or "college"
    return f"{kind}_{college}_{resolved.payout_month:%Y-%m}_DRAFT.xlsx"


def _xlsx(buffer: io.BytesIO, filename: str, report: ValidationReportOut) -> Response:
    """The workbook, plus the headers that stop it being mistaken for approved."""
    codes = ",".join(issue.code.value for issue in report.blocking)
    return Response(
        content=buffer.getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            ARTIFACT_STATE_HEADER: ArtifactState.DRAFT.value,
            BLOCKED_HEADER: "true" if report.is_blocked else "false",
            BLOCKING_CODES_HEADER: codes,
        },
    )
