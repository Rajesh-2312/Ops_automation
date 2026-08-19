"""SQLAlchemy models — a typed *mapping* layer over the schema. It never generates it.

CLAUDE.md §11:

    "Migrations are hand-authored SQL in `supabase/migrations/`, applied in
     filename order. That is the single source of truth for schema. **Not
     Alembic** [...] SQLAlchemy models in `db/` are a typed mapping layer that
     mirrors the schema and never generates it; a test diffs them against
     `information_schema` to catch drift."

Which means, concretely and permanently:

* **No `Base.metadata.create_all()`.** Anywhere. Not in a fixture, not in a
  conftest, not "just for tests". The security posture *is* the RLS policies, the
  `SECURITY DEFINER` helpers and the `auth.users` triggers, and `create_all()`
  reproduces none of them — it would stand up a table that looks right and is
  wide open.
* **No Alembic.** Two migration systems against one database is a defect
  generator (§11).
* **No `CREATE TYPE`.** The enum columns below name Postgres types that already
  exist, declared in `0100_enums_and_utils.sql`. SQLAlchemy is told their labels
  so it can bind and fetch values, not so it can create them.

If a column here disagrees with the SQL, the SQL wins and this file is the bug.
`tests/integration/test_schema_drift.py` diffs the two against
`information_schema` and fails on any difference.

MONEY
-----
Every monetary column is `Numeric`, never `Float` (CLAUDE.md R7), so it arrives
in Python as `Decimal`. `numeric(14,2)` for amounts, `numeric(5,4)` for the TDS
rate, `numeric(6,2)` for `payable_days` — a half day is 0.5 (§5).

ENUMS
-----
Columns whose Postgres enum has a counterpart in `app/domain/enums.py` are mapped
to that enum via `_domain_enum()`, which pins `values_callable` so the **value**
(`"manager"`) is stored rather than the member name (`"MANAGER"`).

Eight Postgres enums have no domain counterpart yet — `trainer_type`,
`attendance_status`, `credentials_status`, `feedback_source`,
`document_category`, `document_status` (flagged in the header of
`0100_enums_and_utils.sql`), and `comms_channel` / `comms_recipient_kind` from
1700. Their labels are spelled out inline via `_pg_enum()` and they map to `str`.
When the domain layer picks them up, swap the call; do not re-derive a second
vocabulary alongside it.

The last two have a `StrEnum` pair in `app/services/comms/types.py` rather than
in `app/domain/enums.py`, and that is a known departure from §11 rather than a
preference — see that module's docstring. It is not imported here: `app/db/`
importing `app/services/` would invert the layering, and the column only needs
the labels.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    types,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.enums import (
    ArtifactState,
    ArtifactType,
    AttendanceMark,
    Corpus,
    DocStatus,
    ErmStatus,
    Persona,
    ProgramStage,
    ProgramType,
    RateBasis,
    ScheduleAnchor,
    TaskCadence,
    TaskStatus,
)


class Base(DeclarativeBase):
    """Declarative base. Its `metadata` is for reflection and drift-diffing only.

    See the module docstring: it is never used to emit DDL.
    """


def _domain_enum(python_enum: type[PyEnum], name: str) -> Enum:
    """A Postgres enum column backed by a `StrEnum` from `app/domain/enums.py`.

    `values_callable` is not optional. Without it SQLAlchemy persists the member
    NAME (`"SENIOR_MANAGER"`), Postgres rejects it as not a label of
    `public.app_role`, and the failure lands at INSERT time in production rather
    than at import time in CI.
    """
    return Enum(
        python_enum,
        name=name,
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


def _pg_enum(name: str, *labels: str) -> Enum:
    """A Postgres enum with no `app/domain/enums.py` counterpart yet.

    The labels are repeated here because the type already exists in Postgres and
    this mapping only needs to bind and fetch its values.
    """
    return Enum(*labels, name=name, native_enum=True)


# --- the enum types, declared once and shared by every column that uses them ---
# One object per Postgres type, so a label added in a migration is added here in
# exactly one place. Reuse across tables is safe: these emit no DDL.

APP_ROLE = _domain_enum(Persona, "app_role")
PROGRAM_TYPE = _domain_enum(ProgramType, "program_type")
PROGRAM_STAGE = _domain_enum(ProgramStage, "program_stage")
RATE_BASIS = _domain_enum(RateBasis, "rate_basis")
TASK_STATUS = _domain_enum(TaskStatus, "task_status")
TASK_CADENCE = _domain_enum(TaskCadence, "task_cadence")
SCHEDULE_ANCHOR = _domain_enum(ScheduleAnchor, "schedule_anchor")
DOC_STATUS = _domain_enum(DocStatus, "doc_status")
ERM_STATUS = _domain_enum(ErmStatus, "erm_status")
ATTENDANCE_MARK = _domain_enum(AttendanceMark, "attendance_mark")
ARTIFACT_STATE = _domain_enum(ArtifactState, "artifact_state")
ARTIFACT_TYPE = _domain_enum(ArtifactType, "artifact_type")

COMMS_CHANNEL = _pg_enum("comms_channel", "email", "whatsapp", "platform_ticket")
COMMS_RECIPIENT_KIND = _pg_enum("comms_recipient_kind", "internal_staff", "trainer", "college")

TRAINER_TYPE = _pg_enum("trainer_type", "freelancer", "full_timer")
ATTENDANCE_STATUS = _pg_enum("attendance_status", "present", "absent", "late", "excused")
CREDENTIALS_STATUS = _pg_enum("credentials_status", "not_created", "created", "shared", "activated")
FEEDBACK_SOURCE = _pg_enum("feedback_source", "platform", "gform")
DOCUMENT_CATEGORY = _pg_enum(
    "document_category",
    "templates_and_masters",
    "college_onboarding",
    "program_planning",
    "trainer_recruitment",
    "trainer_onboarding",
    "student_credentials",
    "trainer_deployment",
    "monitoring_and_quality",
    "feedback_and_assessments",
    "governance_reports",
    "remuneration",
    "invoice_generation",
    "archive",
)
DOCUMENT_STATUS = _pg_enum(
    "document_status", "not_started", "in_progress", "filed", "approved", "not_applicable"
)

#: Named constants for the two `document_*` vocabularies the API writes, so no
#: caller has to spell a status or a category as a bare string literal (§11).
#: These move to `app/domain/enums.py` when that module picks the two enums up.
DOCUMENT_STATUS_NOT_STARTED: Final[str] = "not_started"

#: The categories walled off from an LDE Executive by the `program_documents`
#: policies in `1000_scheduling_documents.sql`. A filed remuneration row is not
#: an amount, but it is a live pointer to one.
COMMERCIAL_DOCUMENT_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"remuneration", "invoice_generation"}
)

#: All money in this schema. R7: Decimal, never float.
MONEY = Numeric(14, 2)

#: `uuid` and `timestamptz`, spelled once.
UUID_PK = PgUUID(as_uuid=True)
TIMESTAMPTZ = DateTime(timezone=True)


# =============================================================================
# 0200 — identity
# =============================================================================


class Profile(Base):
    """`public.profiles` — app identity extending `auth.users`.

    Persona lives here; REACH does not (CLAUDE.md §4). `college_id` serves the
    College persona alone — internal staff take scope from the two assignment
    tables below, never from this column.
    """

    __tablename__ = "profiles"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    role: Mapped[Persona] = mapped_column(APP_ROLE, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False)
    full_name: Mapped[str | None] = mapped_column(Text)
    trainer_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("trainers.id", ondelete="SET NULL")
    )
    college_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("colleges.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


# =============================================================================
# 0300 — org core and the scope graph
# =============================================================================


class Cluster(Base):
    """`public.clusters` — a Senior Manager's unit of scope. Flat, never nested."""

    __tablename__ = "clusters"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class College(Base):
    """`public.colleges`."""

    __tablename__ = "colleges"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    cluster_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("clusters.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str | None] = mapped_column(Text)
    contact_name: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    contact_phone: Mapped[str | None] = mapped_column(Text)
    mou_status: Mapped[DocStatus] = mapped_column(DOC_STATUS, nullable=False)
    po_status: Mapped[DocStatus] = mapped_column(DOC_STATUS, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class UserCollegeAssignment(Base):
    """`public.user_college_assignments` — Manager / LDE Executive to colleges.

    One half of `my_college_ids()`. The app-side mirror is
    `app.core.security.Principal.college_ids`.
    """

    __tablename__ = "user_college_assignments"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    college_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("colleges.id", ondelete="CASCADE"), nullable=False
    )
    assigned_by: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class UserClusterAssignment(Base):
    """`public.user_cluster_assignments` — Senior Manager to clusters.

    Expanded to colleges through `colleges.cluster_id`; the other half of
    `my_college_ids()`.
    """

    __tablename__ = "user_cluster_assignments"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    cluster_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False
    )
    assigned_by: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class Program(Base):
    """`public.programs`. `stage` is an attribute, not a state machine (§5)."""

    __tablename__ = "programs"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    college_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("colleges.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[ProgramType] = mapped_column(PROGRAM_TYPE, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    timetable_url: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[ProgramStage] = mapped_column(PROGRAM_STAGE, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class Batch(Base):
    """`public.batches` — a cohort within a program; the unit a trainer deploys to."""

    __tablename__ = "batches"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    program_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str | None] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(Text)
    #: Graduating year of the cohort. Nullable only for rows predating 1500 —
    #: that migration explains why it is not NOT NULL yet and what closes it.
    passout_year: Mapped[int | None] = mapped_column(Integer)
    expected_student_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class Student(Base):
    """`public.students` — credential-tracking stub. The LMS owns the student record."""

    __tablename__ = "students"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    full_name: Mapped[str | None] = mapped_column(Text)
    roll_number: Mapped[str | None] = mapped_column(Text)
    credentials_status: Mapped[str] = mapped_column(CREDENTIALS_STATUS, nullable=False)
    source_system: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    external_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


# =============================================================================
# 0400 — trainers and deployments
# =============================================================================


class Trainer(Base):
    """`public.trainers`. PAN is the identity key (§6) — never match by name string."""

    __tablename__ = "trainers"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    pan: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(TRAINER_TYPE, nullable=False)
    work_order_status: Mapped[DocStatus] = mapped_column(DOC_STATUS, nullable=False)
    zoho_id: Mapped[str | None] = mapped_column(Text)
    zoho_url: Mapped[str | None] = mapped_column(Text)
    erm_status: Mapped[ErmStatus] = mapped_column(ERM_STATUS, nullable=False)
    erm_external_id: Mapped[str | None] = mapped_column(Text)
    erm_url: Mapped[str | None] = mapped_column(Text)
    erm_synced_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    erm_synced_by: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class TrainerBankAccount(Base):
    """`public.trainer_bank_accounts` — payment rails, 1:1 with a trainer (1400).

    A separate table rather than columns on `trainers` because RLS is row-level
    and 0400 hands an LDE Executive the whole trainer row; the rails sit behind
    `can_see_commercials()` instead (§4, R5). `trainer_id` is the primary key.
    """

    __tablename__ = "trainer_bank_accounts"

    trainer_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("trainers.id", ondelete="CASCADE"), primary_key=True
    )
    bank_account_number: Mapped[str] = mapped_column(Text, nullable=False)
    ifsc: Mapped[str] = mapped_column(Text, nullable=False)
    bank_name: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    account_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class Deployment(Base):
    """`public.deployments` — which trainer teaches which batch, from when.

    The join that defines trainer visibility and the grain of
    `trainer_attendance`.
    """

    __tablename__ = "deployments"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    trainer_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("trainers.id", ondelete="RESTRICT"), nullable=False
    )
    batch_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    tracksheet_url: Mapped[str | None] = mapped_column(Text)
    travel_notes: Mapped[str | None] = mapped_column(Text)
    travel_submitted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


# =============================================================================
# 0500 / 1000 — the checklist engine and the document register
# =============================================================================


class TaskTemplate(Base):
    """`public.task_templates` — the library behind `POST /programs/{id}/tasks:generate`.

    `offset_days` / `offset_anchor` arrived in `1000_scheduling_documents.sql` and
    are what turn a checklist into a schedule. Resolving them against a program's
    dates is `app/api/scheduling.py`'s job and deliberately not a trigger's — a
    program's dates may still be NULL during `acquisition_setup`.
    """

    __tablename__ = "task_templates"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    stage: Mapped[ProgramStage] = mapped_column(PROGRAM_STAGE, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    default_owner_role: Mapped[Persona] = mapped_column(APP_ROLE, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    cadence: Mapped[TaskCadence] = mapped_column(TASK_CADENCE, nullable=False)
    #: NULL means "applies to every program type".
    applies_to_type: Mapped[ProgramType | None] = mapped_column(PROGRAM_TYPE)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Days from the anchor. Negative = before. NULL = no automatic due date.
    offset_days: Mapped[int | None] = mapped_column(Integer)
    offset_anchor: Mapped[ScheduleAnchor] = mapped_column(SCHEDULE_ANCHOR, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class Task(Base):
    """`public.tasks` — a checklist item on a program.

    NOTE for anyone writing here on a service-role connection: the
    `tasks_guard_and_stamp` BEFORE UPDATE trigger short-circuits on
    `auth.uid() is null`, so it neither protects this table from us nor stamps
    `completed_at` for us. Both become the API's responsibility. See
    `app/core/security.py`.
    """

    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    program_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="CASCADE"), nullable=False
    )
    template_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("task_templates.id", ondelete="SET NULL")
    )
    stage: Mapped[ProgramStage] = mapped_column(PROGRAM_STAGE, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    owner_role: Mapped[Persona | None] = mapped_column(APP_ROLE)
    status: Mapped[TaskStatus] = mapped_column(TASK_STATUS, nullable=False)
    cadence: Mapped[TaskCadence] = mapped_column(TASK_CADENCE, nullable=False)
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    completed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    waiting_on: Mapped[str | None] = mapped_column(Text)
    source_system: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    external_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class DocumentTemplate(Base):
    """`public.document_templates` — the `00_Templates_and_Masters` master library."""

    __tablename__ = "document_templates"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    category: Mapped[str] = mapped_column(DOCUMENT_CATEGORY, nullable=False)
    #: NULL for categories that are not stage work (the master library, archive).
    stage: Mapped[ProgramStage | None] = mapped_column(PROGRAM_STAGE)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    master_url: Mapped[str | None] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    applies_to_type: Mapped[ProgramType | None] = mapped_column(PROGRAM_TYPE)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class ProgramDocument(Base):
    """`public.program_documents` — what each program owes, per document.

    `category` is copied from the template rather than joined because it is a
    SECURITY input: the policies in `1000_scheduling_documents.sql` wall off
    `remuneration` and `invoice_generation` from an LDE Executive. The generate
    endpoint honours the same split in code — see `app/api/programs.py`.
    """

    __tablename__ = "program_documents"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    program_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="CASCADE"), nullable=False
    )
    document_template_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("document_templates.id", ondelete="SET NULL")
    )
    category: Mapped[str] = mapped_column(DOCUMENT_CATEGORY, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(DOCUMENT_STATUS, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    filed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


# =============================================================================
# 0600 — monitoring
# =============================================================================


class Observation(Base):
    """`public.observations` — classroom observation of a deployment."""

    __tablename__ = "observations"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    deployment_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False
    )
    observer_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    observed_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class Feedback(Base):
    """`public.feedback` — a pointer to a feedback collection. Responses stay external."""

    __tablename__ = "feedback"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(FEEDBACK_SOURCE, nullable=False)
    collected_on: Mapped[dt.date | None] = mapped_column(Date)
    external_url: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    #: Not money, but still Numeric — a score is a decimal, and float would drift
    #: it the same way (R7 in spirit).
    summary_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    response_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class Assessment(Base):
    """`public.assessments` — ClickUp owns the work item; we hold the links."""

    __tablename__ = "assessments"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    conducted_on: Mapped[dt.date | None] = mapped_column(Date)
    clickup_url: Mapped[str | None] = mapped_column(Text)
    report_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class AttendanceRecord(Base):
    """`public.attendance_records` — STUDENT attendance. Not a payout input.

    Distinct from `TrainerAttendance` in grain, vocabulary, security class and
    blast radius. Read the header of `0600_monitoring.sql` before merging them.
    """

    __tablename__ = "attendance_records"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    deployment_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("students.id", ondelete="CASCADE")
    )
    session_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(ATTENDANCE_STATUS, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class TrainerAttendance(Base):
    """`public.trainer_attendance` — one trainer-day mark. The input to payable days.

    CLAUDE.md §6: one row per day, never a wide D1-D31 layout, because payout
    disputes are always about specific days. There is no `created_at` on this
    table — `marked_at` is it, and it is the operational timestamp of the mark.
    """

    __tablename__ = "trainer_attendance"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    deployment_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False
    )
    mark_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    mark: Mapped[AttendanceMark] = mapped_column(ATTENDANCE_MARK, nullable=False)
    marked_by: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    marked_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


# =============================================================================
# 0700 — the commercials wall
# =============================================================================
# Reading any of the four tables below on a service-role connection returns EVERY
# row: `can_see_commercials()` is not evaluated for us. Guard every query with
# `require_commercials()` AND `require_college_reach()` — both conjuncts, exactly
# as the policies are written.


class WorkOrder(Base):
    """`public.work_orders` — the signed contract, and the home of the RATE.

    COMMERCIAL. The rate lives here rather than on `deployments` precisely so an
    LDE Executive can read deployments without reading rates (§4).
    """

    __tablename__ = "work_orders"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    trainer_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("trainers.id", ondelete="RESTRICT"), nullable=False
    )
    program_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="RESTRICT"), nullable=False
    )
    rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    rate_basis: Mapped[RateBasis] = mapped_column(RATE_BASIS, nullable=False)
    valid_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[DocStatus] = mapped_column(DOC_STATUS, nullable=False)
    signed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    document_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class RemunerationSheet(Base):
    """`public.remuneration_sheets` — one computed payout, one trainer, one period.

    Columns mirror CLAUDE.md §6 line for line, in the order the formula runs.
    Every figure is written by `app/services/remuneration/engine.py` (R2) — the
    API persists what the engine returns and computes nothing itself.
    """

    __tablename__ = "remuneration_sheets"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    trainer_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("trainers.id", ondelete="RESTRICT"), nullable=False
    )
    program_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="RESTRICT"), nullable=False
    )
    work_order_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("work_orders.id", ondelete="SET NULL")
    )
    period_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[dt.date] = mapped_column(Date, nullable=False)

    # Inputs, snapshotted at computation time — never joined at read time.
    rate: Mapped[Decimal | None] = mapped_column(MONEY)
    rate_basis: Mapped[RateBasis | None] = mapped_column(RATE_BASIS)
    #: Numeric, not Integer: a half day is 0.5 (§5).
    payable_days: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    days_in_month: Mapped[int | None] = mapped_column(Integer)

    earned: Mapped[Decimal | None] = mapped_column(MONEY)
    ta_da: Mapped[Decimal | None] = mapped_column(MONEY)
    accommodation: Mapped[Decimal | None] = mapped_column(MONEY)
    travel_reimb: Mapped[Decimal | None] = mapped_column(MONEY)
    gross: Mapped[Decimal | None] = mapped_column(MONEY)
    #: TDS base is EARNED, never gross — reimbursements are not income (§6).
    tds_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    tds: Mapped[Decimal | None] = mapped_column(MONEY)
    deductions: Mapped[Decimal | None] = mapped_column(MONEY)
    #: The only rounded figure in the row (R6: round once, at net).
    net_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    amount_in_words: Mapped[str | None] = mapped_column(Text)

    currency: Mapped[str] = mapped_column(Text, nullable=False)
    payout_status: Mapped[DocStatus] = mapped_column(DOC_STATUS, nullable=False)
    paid_on: Mapped[dt.date | None] = mapped_column(Date)

    #: Snapshot of `trainers.pan`, not a join: an issued invoice number is frozen.
    invoice_pan: Mapped[str | None] = mapped_column(Text)
    invoice_fy: Mapped[str | None] = mapped_column(Text)
    invoice_month: Mapped[str | None] = mapped_column(Text)
    invoice_seq: Mapped[int | None] = mapped_column(Integer)
    invoice_no: Mapped[str | None] = mapped_column(Text)
    invoice_issued_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)

    source_system: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    external_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class GovernanceReport(Base):
    """`public.governance_reports`.

    Not a commercial table. `shared_with_college_at` is the publish gate (R4):
    until it is set, the report is internal.
    """

    __tablename__ = "governance_reports"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    program_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    reporting_period_start: Mapped[dt.date | None] = mapped_column(Date)
    reporting_period_end: Mapped[dt.date | None] = mapped_column(Date)
    shared_with_college_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    shared_by: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class Pnl(Base):
    """`public.pnl` — program profit & loss. Senior Manager and Manager only."""

    __tablename__ = "pnl"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    program_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="CASCADE"), nullable=False
    )
    revenue: Mapped[Decimal | None] = mapped_column(MONEY)
    trainer_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    travel_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    platform_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    other_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    accrued_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    invoiced_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


# =============================================================================
# 1300 — the audit trail and the R4 artifact lifecycle
# =============================================================================
# Two mappings with sharper edges than anything above, both of them enforced in
# `1300_audit_and_artifact_versions.sql` by triggers rather than by policy —
# which means they apply to THIS connection too, BYPASSRLS or not. Read that
# migration before writing against either table.


class AuditEventRow(Base):
    """`public.audit_events` — CLAUDE.md §11: actor, action, before, after, at.

    Named `AuditEventRow`, not `AuditEvent`, because `app.core.audit.AuditEvent`
    is the Pydantic model this table stores and the two would otherwise be one
    import away from being confused. The Pydantic model is what callers build;
    this is the row it lands in, and `app/core/audit.py` is the only writer.

    **APPEND-ONLY, and the database means it.** No UPDATE and no DELETE, for
    anyone, enforced by `audit_events_append_only()` — a trigger, so the
    service-role connection this file's session uses does not step around it.
    A `session.delete(row)` or an ORM attribute assignment followed by a flush
    will raise `42501`. That is not a bug to work around; record a correcting
    event instead.

    NOTE `actor_id` and `entity_id` carry no ForeignKey, deliberately. See the
    column comments in the migration: every available ON DELETE action loses
    either the audit row or the actor on it, and `entity_id` is polymorphic
    across every table in the schema.
    """

    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    actor_id: Mapped[UUID | None] = mapped_column(UUID_PK)
    #: The actor's persona AS IT WAS. Snapshotted, never joined — a promotion
    #: must not restate who was allowed to do what last quarter.
    actor_persona: Mapped[Persona | None] = mapped_column(APP_ROLE)
    #: Open vocabulary, `text` not an enum, so recording a new audit line is
    #: never a schema change. `app.core.audit.AuditAction` names what exists.
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_table: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(UUID_PK)
    #: Row snapshots. `dict[str, Any]` rather than a typed model on purpose —
    #: these are a serialised record, not a contract between layers, and they
    #: hold whatever the entity's columns were. May contain commercials, which
    #: is why the read policies on this table are as narrow as they are.
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class ArtifactVersion(Base):
    """`public.artifact_versions` — the CLAUDE.md R4 lifecycle store.

    DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED, generic over artifact
    type. This table holds the lifecycle and the hash; the artifact's CONTENT
    stays in `remuneration_sheets` / `governance_reports` / `program_documents`,
    which remain the systems of record.

    Three things the database enforces that application code must not assume it
    is free to work around:

    * **Approval freezes.** Once `state` is APPROVED or RELEASED,
      `artifact_type`, `artifact_id`, `version`, `created_at`, `content_hash`,
      `approved_by` and `approved_at` are immutable, and the row cannot be walked
      back to DRAFT. Editing an approved artifact means inserting a NEW version.
    * **Exactly one current version per artifact** — `superseded_at is null` is
      "current", and a partial unique index enforces the one. Supersede the old
      row in the same transaction that inserts the new one, or the insert loses.
    * **Approval and release are separate acts.** Two actors, two timestamps, two
      audit rows (R4). Do not populate the release columns at approval time; a
      CHECK constraint rejects it.

    `content_hash` is NULL until approval and non-NULL from then on, in both
    directions. The transition table itself is NOT in SQL — it lives in
    `app.domain.enums.ALLOWED_TRANSITIONS` and `app/services/approval/`, because
    restating it in a second language is how two specifications drift apart.
    """

    __tablename__ = "artifact_versions"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    #: The enum's values ARE the target table names (`app.domain.enums`).
    artifact_type: Mapped[ArtifactType] = mapped_column(ARTIFACT_TYPE, nullable=False)
    #: No ForeignKey: the target table varies by row. `artifact_type` names it.
    artifact_id: Mapped[UUID] = mapped_column(UUID_PK, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[ArtifactState] = mapped_column(ARTIFACT_STATE, nullable=False)
    #: Frozen at approval (R4). NULL before it, immutable after it.
    content_hash: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[UUID | None] = mapped_column(UUID_PK)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    submitted_by: Mapped[UUID | None] = mapped_column(UUID_PK)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    approved_by: Mapped[UUID | None] = mapped_column(UUID_PK)
    approved_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    released_by: Mapped[UUID | None] = mapped_column(UUID_PK)
    released_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)

    #: NULL means "this is the current version".
    superseded_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


# =============================================================================
# 1600 — the RAG corpora (CLAUDE.md §9)
# =============================================================================
# Four tables behind the Ops Copilot. Read `1600_rag_corpora.sql` before writing
# against any of them; three properties there are enforced in SQL and are not
# this file's to relax:
#
#   * `rag_chunks.section` is NOT NULL, because §9 requires every answer to cite
#     document AND section. A chunk that cannot be cited must not be retrievable.
#   * A document VERSION is a row. Superseding preserves the old text and flags
#     it; it never overwrites (§9: "a superseded clause must not surface without
#     a version flag").
#   * `rag_embeddings` has no grant to `authenticated` at all. Similarity search
#     runs server-side through `public.rag_search()`, which is the only path in
#     this system that returns neighbours — and it takes the persona as required
#     parameters, so retrieval cannot precede filtering.


class Vector(types.UserDefinedType[list[float]]):
    """Minimal mapping for pgvector's `vector(n)`.

    Hand-rolled rather than `from pgvector.sqlalchemy import Vector` on purpose:
    the mapping layer needs to bind and fetch the type, nothing more, and one
    small class is cheaper than a dependency in the lock file for a column no
    application query ever selects. It emits no DDL of its own — like every type
    in this module, the Postgres type already exists (created by 1600).

    Note the deliberate asymmetry with money: R7 forbids float for currency, and
    an embedding is not currency. It is a direction in a metric space, where
    `Decimal` would be both meaningless and 50x slower.
    """

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(
        self,
        **_kw: Any,  # noqa: ANN401  # SQLAlchemy's hook signature; the kwargs are the dialect's
    ) -> str:
        return f"vector({self.dim})"

    def bind_processor(self, dialect: Dialect) -> Callable[[list[float] | None], str | None]:
        def process(value: list[float] | None) -> str | None:
            if value is None:
                return None
            return format_vector(value)

        return process

    def result_processor(
        self, dialect: Dialect, coltype: object
    ) -> Callable[[str | None], list[float] | None]:
        def process(value: str | None) -> list[float] | None:
            if value is None:
                return None
            return [float(part) for part in value.strip("[]").split(",") if part]

        return process


#: The embedding width every vector in this schema has. Pinned in one place
#: because it appears in three: the column type, the `rag_embeddings_dim_ck`
#: CHECK constraint, and the `::vector(1536)` cast inside `public.rag_search()`.
#: A model with a different width needs a migration, not a constant edit.
EMBEDDING_DIM: Final[int] = 1536

RAG_CORPUS = _domain_enum(Corpus, "rag_corpus")


def format_vector(values: Sequence[float]) -> str:
    """A vector in pgvector's text form, `[0.1,0.2,...]`.

    Exported because `public.rag_search()` takes the query vector as **text** and
    casts it inside the function — so no client-side driver has to know the
    extension type, and the cast happens where the search_path is correct.
    `repr` on a Python float round-trips exactly, so this loses nothing.
    """
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


class RagCorpusAccess(Base):
    """`public.rag_corpus_access` — which persona may read which corpus (§9).

    Reference data seeded by migration 1600, like `task_templates`. There is no
    write path from the application: a persona that could INSERT here could grant
    itself the Contracts corpus. Changes are migrations, under review.
    """

    __tablename__ = "rag_corpus_access"

    corpus: Mapped[Corpus] = mapped_column(RAG_CORPUS, primary_key=True)
    role: Mapped[Persona] = mapped_column(APP_ROLE, primary_key=True)
    #: Why this persona holds this corpus. The column an access review reads.
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class RagDocument(Base):
    """`public.rag_documents` — one row per document VERSION.

    `source_ref` is the identity ACROSS versions; `(corpus, source_ref, version)`
    is unique and `superseded_at is null` marks the current one, enforced by a
    partial unique index. A July payout dispute is argued against the work order
    as it read in July, so superseding never overwrites.

    `content_hash` is what makes ingestion idempotent: re-ingesting an unchanged
    version is a no-op rather than a re-chunk and a re-embed.
    """

    __tablename__ = "rag_documents"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    corpus: Mapped[Corpus] = mapped_column(RAG_CORPUS, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    #: The document half of a §9 citation. NOT NULL, deliberately.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: NULL means current. §9's version flag.
    superseded_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)

    #: NULL means organisation-wide (an SOP). Non-NULL narrows to the assignment
    #: graph, exactly like every other scoped table.
    college_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("colleges.id", ondelete="CASCADE")
    )
    program_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="CASCADE")
    )

    #: §4's wall at document granularity — reading requires commercials.
    is_commercial: Mapped[bool] = mapped_column(Boolean, nullable=False)

    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    ingested_by: Mapped[UUID | None] = mapped_column(UUID_PK)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class RagChunk(Base):
    """`public.rag_chunks` — the retrievable unit.

    `section` is NOT NULL because §9 requires a citation to name document and
    section, and the cheapest place to guarantee a citable chunk is the column.
    `ordinal` is deterministic: `app/rag/chunking.py` is a pure function of the
    source text, which is what lets a re-ingest produce identical chunks.
    """

    __tablename__ = "rag_chunks"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("rag_documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The section half of a §9 citation.
    section: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: §4's wall at chunk granularity — text about money is still walled. NOT a
    #: claim that the number is correct or current; R1 stands.
    is_commercial: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class RagEmbedding(Base):
    """`public.rag_embeddings` — one vector per (chunk, model).

    `model` is part of the primary key because two models' vectors are not
    comparable, and mixing them in one nearest-neighbour scan yields plausible
    wrong neighbours — the worst failure mode retrieval has.

    No grant to `authenticated`: an embedding is a lossy encoding of the chunk
    text and would carry a blurred copy of the Contracts corpus past the wall the
    chunk policy enforces. Server-side search only.
    """

    __tablename__ = "rag_embeddings"

    chunk_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("rag_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


# =============================================================================
# 1700 — the Comms Service outbound queue (CLAUDE.md §8)
# =============================================================================


class CommsMessage(Base):
    """`public.comms_messages` — the single outbound queue (CLAUDE.md §8).

    "Channel, recipient, template, and diff-from-template shown at approval."
    All four are columns, and the diff is stored rather than recomputed at read
    time so that a later template edit cannot restate what an approver was shown.

    **This table carries its own R4 lifecycle columns, and that is a documented
    departure rather than a duplication.** `artifact_versions` would be the right
    home, but `artifact_versions.artifact_type` is a closed enum mirroring
    `app.domain.enums.ArtifactType`, and adding a label to only one half of that
    mirror breaks every read. `1700_comms_queue.sql` states the whole argument and
    names the migration that undoes it once §14 Q3 is answered. What is NOT
    duplicated is the grammar: transitions go through
    `app.services.approval.state_machine.check_transition()` and the freeze uses
    `app.services.approval.hashing.content_hash()`, the same functions that govern
    a remuneration sheet.

    Three things the database enforces here, mirroring 1300:

    * **Approval freezes.** Once APPROVED, the recipient, channel, template,
      body, diff, hash and approver are immutable and the row cannot walk back to
      DRAFT. Revising means inserting a new DRAFT with `supersedes_id` set.
    * **Release is a separate act.** `released_by` / `released_at` are their own
      pair with their own CHECK, and RELEASED means "an authenticated human
      cleared this to go" — **not** that anything was transmitted. No provider is
      wired in this phase and there is deliberately no `sent_at` column.
    * **The commercials wall is a column.** `is_commercial` is declared by the
      drafter because no SQL predicate can read prose, and forced true whenever
      the message points at a remuneration artifact.
    """

    __tablename__ = "comms_messages"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)
    program_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="CASCADE"), nullable=False
    )

    #: `str`, not a domain enum — see the module docstring. The `StrEnum` pair is
    #: `app.services.comms.types`, and its members are `str`, so assigning one
    #: here binds correctly without this layer importing that one.
    channel: Mapped[str] = mapped_column(COMMS_CHANNEL, nullable=False)
    #: Sets the §8 autonomy ceiling: internal chase and a college contact are not
    #: the same risk, and an email address alone cannot tell them apart.
    recipient_kind: Mapped[str] = mapped_column(COMMS_RECIPIENT_KIND, nullable=False)
    recipient_ref: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_name: Mapped[str | None] = mapped_column(Text)

    template_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: The rendered BASELINE — template plus `template_values`, nothing else.
    #: The left-hand side of the approval diff.
    template_body: Mapped[str] = mapped_column(Text, nullable=False)
    #: R1: the structured input every fact in the message came from. A figure in
    #: `body` that is absent here was invented by the drafter.
    template_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: The §8 review surface, as `app.services.comms.diff.TemplateDiff` renders
    #: it. Frozen with the rest at approval.
    diff: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    is_commercial: Mapped[bool] = mapped_column(Boolean, nullable=False)
    related_artifact_type: Mapped[ArtifactType | None] = mapped_column(ARTIFACT_TYPE)
    related_artifact_id: Mapped[UUID | None] = mapped_column(UUID_PK)

    state: Mapped[ArtifactState] = mapped_column(ARTIFACT_STATE, nullable=False)
    #: Frozen at approval (R4). NULL before it, immutable after it.
    content_hash: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[UUID | None] = mapped_column(UUID_PK)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    submitted_by: Mapped[UUID | None] = mapped_column(UUID_PK)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    approved_by: Mapped[UUID | None] = mapped_column(UUID_PK)
    approved_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    released_by: Mapped[UUID | None] = mapped_column(UUID_PK)
    released_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("comms_messages.id", ondelete="SET NULL")
    )
    superseded_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)

    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
