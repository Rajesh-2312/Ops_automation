"""Canonical enums for the byteXL Ops platform.

CLAUDE.md §11: "Enums in `domain/`, never string literals for status values."
CLAUDE.md §3:  this module — like everything in `domain/` — imports nothing from
`db/`, `api/`, or `agents/`. Business rules stay testable without a database.

Every `StrEnum` value here is the exact string stored in Postgres, so the Python
enum and the SQL enum are the same vocabulary. Changing a value is a migration,
not a refactor.

**`StrEnum`, never `(str, Enum)`.** They compare identically, so the difference
hides until it costs something: on 3.11+ a `(str, Enum)` member formats as
`"Persona.MANAGER"` under `f"{...}"`, `str()` and `"".join()`, while `==` keeps
passing. Any f-string that reaches SQL, a filename or a sheet cell would write
the wrong token silently. `StrEnum` renders `"manager"`. Do not convert back.

`AutonomyLevel` stays `(int, Enum)` — it is ordered, not a stored string.
"""

from __future__ import annotations

from enum import Enum, StrEnum


class Persona(StrEnum):
    """The five personas of CLAUDE.md §4.

    Hierarchy: SENIOR_MANAGER -> MANAGER -> LDE_EXECUTIVE (on campus).
    TRAINER and COLLEGE are external-facing and sit outside that chain.

    Scope is NOT stored here. Internal staff are bound to colleges through
    `user_college_assignments` / `user_cluster_assignments`; the database
    enforces reach via RLS helper functions, not application code.
    """

    SENIOR_MANAGER = "senior_manager"
    MANAGER = "manager"
    LDE_EXECUTIVE = "lde_executive"
    TRAINER = "trainer"
    COLLEGE = "college"


#: Personas that may see commercials — P&L, remuneration, invoices, work-order
#: rates. CLAUDE.md §4 is explicit that an LDE Executive has **no commercials**.
#: This tuple mirrors the SQL function `public.can_see_commercials()`; the
#: database is the enforcement point and this is the app-side echo of it.
COMMERCIALS_PERSONAS: frozenset[Persona] = frozenset({Persona.SENIOR_MANAGER, Persona.MANAGER})

#: Personas that are byteXL staff rather than external parties.
INTERNAL_PERSONAS: frozenset[Persona] = frozenset(
    {Persona.SENIOR_MANAGER, Persona.MANAGER, Persona.LDE_EXECUTIVE}
)


class ProgramType(StrEnum):
    """CLAUDE.md §5 — the central branch.

    The two types differ in rate basis, and that difference propagates into
    attendance semantics. See `RateBasis` and `app.domain.attendance`.
    """

    CRT = "CRT"
    BCAP = "bCAP"


class RateBasis(StrEnum):
    """How a trainer's engagement rate is expressed.

    CRT engagements are per day; bCAP engagements are per month, prorated by
    calendar days. The basis lives on the work order, not on the program type,
    because the work order is the signed contract — but in practice CRT implies
    PER_DAY and bCAP implies PER_MONTH (validated as a §7 gate).
    """

    PER_DAY = "per_day"
    PER_MONTH = "per_month"


class AttendanceMark(StrEnum):
    """A single trainer-day mark. One row per day, never a wide D1-D31 layout.

    CLAUDE.md §6: "Payout disputes are always about specific days."

    The payable-day consequences are asymmetric between program types and are
    deliberately NOT encoded here — see `app.domain.attendance`, which owns the
    CRT-counts-up / bCAP-counts-down branch. Keeping the semantics out of the
    enum stops anyone from assuming a mark means the same thing on both paths.
    """

    PRESENT = "P"
    ABSENT = "A"
    HALF_DAY = "H"
    HOLIDAY = "HOLIDAY"
    UNMARKED = "UNMARKED"


class ArtifactState(StrEnum):
    """CLAUDE.md R4 — nothing leaves the system unapproved.

    Every artifact moves DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED.
    Approval freezes and hashes the version. Editing an approved artifact
    creates a NEW version in DRAFT requiring fresh approval.

    APPROVED and RELEASED are separate states because approval and release are
    separate actions with separate audit rows.
    """

    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    RELEASED = "RELEASED"


#: The only legal transitions. Anything not listed is a defect, not an edge case.
#: An edit to an APPROVED artifact does not appear here: it does not transition
#: the artifact, it creates a new version that starts again at DRAFT.
ALLOWED_TRANSITIONS: dict[ArtifactState, frozenset[ArtifactState]] = {
    ArtifactState.DRAFT: frozenset({ArtifactState.PENDING_APPROVAL}),
    ArtifactState.PENDING_APPROVAL: frozenset(
        {ArtifactState.APPROVED, ArtifactState.DRAFT}  # rejection returns to DRAFT
    ),
    ArtifactState.APPROVED: frozenset({ArtifactState.RELEASED}),
    ArtifactState.RELEASED: frozenset(),  # terminal
}


class ArtifactType(StrEnum):
    """What kind of thing is moving through the R4 lifecycle.

    R4 says "every artifact", so the approval machinery is generic over type
    rather than reimplemented per table. This enum is the closed vocabulary: an
    artifact type that is not listed here cannot be approved, which is the
    correct failure for something nobody has decided the approval authority for.

    The value doubles as the `entity_table` on the audit row, so it is the
    Postgres table name and not a prettier synonym — §11 puts the table name on
    the audit event "so a row can be found again without guessing".
    """

    REMUNERATION_SHEET = "remuneration_sheets"
    GOVERNANCE_REPORT = "governance_reports"
    PROGRAM_DOCUMENT = "program_documents"


#: Who may approve each artifact type. R4 and §4.
#:
#: `REMUNERATION_SHEET` is Senior Manager only — §4 puts "payout approval" in
#: that persona's column explicitly.
#:
#: The other two are deliberately absent rather than guessed. §14 Q3 is open:
#: "Approval authority for college-facing comms: Manager or Senior Manager?" A
#: missing entry raises in `approver_personas()`, so an unanswered governance
#: question surfaces as a loud failure at the approval attempt rather than as a
#: quiet default that nobody reviews. Fill these in when Q3 is answered — do not
#: pick the permissive option to make a test pass.
APPROVAL_AUTHORITY: dict[ArtifactType, frozenset[Persona]] = {
    ArtifactType.REMUNERATION_SHEET: frozenset({Persona.SENIOR_MANAGER}),
}


class ProgramStage(StrEnum):
    """The six pipeline stages. An attribute, not a state machine — tasks within
    a stage run in parallel."""

    ACQUISITION_SETUP = "acquisition_setup"
    TRAINER_SOURCING = "trainer_sourcing"
    TRAINER_ONBOARDING = "trainer_onboarding"
    DEPLOYMENT = "deployment"
    ACTIVE_MONITORING = "active_monitoring"
    CLOSEOUT_FINANCE = "closeout_finance"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


class DocStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SENT = "sent"
    SIGNED = "signed"
    EXPIRED = "expired"


class ErmStatus(StrEnum):
    """CLAUDE.md §10 — ERM is external with no API, synced by a human paste.

    STALE is the one that matters: if the local record changes after sync, the
    record flips to STALE and requeues. Without drift detection the two systems
    diverge within a month and neither is trusted.
    """

    NOT_PUSHED = "not_pushed"
    PENDING = "pending"
    SYNCED = "synced"
    STALE = "stale"
    FAILED = "failed"


class TaskCadence(StrEnum):
    """A label, not a scheduler. Nothing generates repeat instances from this."""

    ONE_TIME = "one_time"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class ScheduleAnchor(StrEnum):
    """Which program date a task template's `offset_days` is measured from."""

    PROGRAM_START = "program_start"
    PROGRAM_END = "program_end"


class ValidationSeverity(StrEnum):
    """CLAUDE.md §7.

    BLOCKING stops a payout cycle reaching PENDING_APPROVAL outright.
    WARNING permits it, but only with a stated reason recorded against the run.
    """

    BLOCKING = "blocking"
    WARNING = "warning"


class ValidationCode(StrEnum):
    """Stable identifiers for the CLAUDE.md §7 payout gates.

    Codes rather than message strings, because the message is what a human reads
    and the code is what the audit row, the UI filter and the "stated reason"
    record key off. Rewording a message must not orphan every historical
    override reason attached to that gate, so the code never changes once it has
    been written to a row — same rule as the `StrEnum` values above: changing
    one is a migration, not a refactor.

    Severity is NOT encoded here. It lives on the gate implementation in
    `app.services.remuneration.validators`, because the same condition changes
    severity by program type: attendance completeness is BLOCKING for CRT and
    WARNING for bCAP (§5, §7). Pinning severity to the code would force two codes
    for one condition and lose the ability to report "attendance incomplete"
    across both program types.
    """

    # --- Blocking (§7): the cycle cannot reach PENDING_APPROVAL --------------
    WORK_ORDER_MISSING = "work_order_missing"
    WORK_ORDER_PERIOD_MISMATCH = "work_order_period_mismatch"
    ZOHO_ACCOUNT_MISSING = "zoho_account_missing"
    PAN_INVALID = "pan_invalid"
    BANK_ACCOUNT_MISSING = "bank_account_missing"
    IFSC_INVALID = "ifsc_invalid"
    PAYABLE_DAYS_EXCEED_MONTH = "payable_days_exceed_month"
    ATTENDANCE_INCOMPLETE = "attendance_incomplete"
    INVOICE_NUMBER_ALREADY_ISSUED = "invoice_number_already_issued"
    RATE_MISMATCH_WITH_WORK_ORDER = "rate_mismatch_with_work_order"
    NET_PAY_NOT_POSITIVE = "net_pay_not_positive"

    # --- Warning (§7): permitted, but only with a stated reason -------------
    NET_DEVIATION_FROM_TRAILING_AVERAGE = "net_deviation_from_trailing_average"
    REIMBURSEMENT_WITH_ZERO_PAYABLE_DAYS = "reimbursement_with_zero_payable_days"


class LLMTask(StrEnum):
    """Routing key for `app.core.llm`. CLAUDE.md §2: "route by task, not by
    default."

    VOLUME tasks go to a cheap fast model; FRONTIER tasks to a stronger one.
    The mapping lives in `app.core.llm.TASK_TIER`, and the model ids behind
    each tier come from env so routing changes without a code deploy.
    """

    DRAFTING = "drafting"
    CHASE = "chase"
    SUMMARY = "summary"
    EXTRACTION = "extraction"
    GOVERNANCE_REPORT = "governance_report"


class ModelTier(StrEnum):
    VOLUME = "volume"
    FRONTIER = "frontier"


class AutonomyLevel(int, Enum):
    """CLAUDE.md §8 — the autonomy ladder.

    "Nothing touching money, contracts, or a college contact goes past level 3."
    """

    OBSERVE = 1
    DRAFT = 2
    ACT_WITH_APPROVAL = 3
    ACT = 4


class Corpus(StrEnum):
    """The six separately-indexed, separately-permissioned RAG corpora (§9)."""

    SOP = "sop"
    CONTRACTS = "contracts"
    COLLEGE_DOSSIER = "college_dossier"
    EDUCATOR = "educator"
    CURRICULUM = "curriculum"
    REPORTS = "reports"
