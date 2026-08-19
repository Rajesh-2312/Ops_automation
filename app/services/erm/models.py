"""The SQLAlchemy mapping for `public.erm_sync_tasks`.

=============================================================================
THIS FILE BELONGS IN `app/db/models.py`, AND SAYS SO ON PURPOSE
=============================================================================
CLAUDE.md §3 puts the typed mapping layer in `app/db/`, and §11 says a test diffs
it against `information_schema` to catch drift. `erm_sync_tasks` should be a
class in `app/db/models.py` beside `CommsMessage`, and the five `programs.erm_*`
columns added by `1900_erm_sync.sql` should be five more lines on `Program`.

`app/db/models.py` is closed to this workstream. The established response in this
codebase is to put the thing beside its only consumer with a docstring that names
where it belongs and what moving it will cost —
`app/services/comms/types.py` did exactly this for two enums. This is that, for
one table.

**When `app/db/models.py` opens:** move `ErmSyncTask` into it verbatim (it
already subclasses the shared `Base`, so it is a cut and paste, not a rewrite),
add the five `erm_*` columns to `Program`, delete this module and re-export
nothing. Nothing else changes: `app/api/erm.py` imports the name from
`app.services.erm`, which is where it will keep coming from either way.

**Until then, know what is not covered.** `tests/integration/test_schema_drift.py`
does not exist yet — `tests/integration/` holds only `__init__.py` — so no test
compares either half against a live `information_schema` today. When one lands it
will find the `programs.erm_*` columns unmapped, and that finding is correct: it
is this workstream's debt, recorded here rather than discovered.

=============================================================================
IT SUBCLASSES `Base`, WHICH IS DELIBERATE AND SAFE
=============================================================================
Registering on the shared `Base.metadata` is the point: it is the metadata a
future drift test reflects against, so this table is discoverable by exactly the
mechanism §11 describes rather than hiding in a private `MetaData` a drift test
would never look at.

That registration emits no DDL and cannot. `app/db/models.py`'s own docstring is
categorical — **no `Base.metadata.create_all()`, anywhere, not in a fixture, not
"just for tests"** — because the security posture IS the RLS policies, the
`SECURITY DEFINER` helpers and the triggers, and `create_all()` reproduces none
of them. It would stand up a table that looks right and is wide open. The schema
comes from `supabase/migrations/1900_erm_sync.sql` and from nowhere else.

No `CREATE TYPE` either: the two Postgres enums below already exist, declared in
that migration. SQLAlchemy is told their labels so it can bind and fetch values,
not so it can create them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import TIMESTAMPTZ, UUID_PK, Base
from app.services.erm.types import ErmSubjectKind, ErmSyncState

__all__ = ["ERM_SUBJECT_KIND", "ERM_SYNC_STATE", "ErmSyncTask"]


def _pg_enum(python_enum: type[ErmSubjectKind] | type[ErmSyncState], name: str) -> Enum:
    """A Postgres enum column backed by one of this package's `StrEnum`s.

    `values_callable` is not optional, for the reason `app/db/models.py` gives
    where it defines the same helper: without it SQLAlchemy persists the member
    NAME (`"QUEUED"`), Postgres rejects it as not a label of
    `public.erm_sync_state`, and the failure lands at INSERT time in production
    rather than at import time in CI.
    """
    return Enum(
        python_enum,
        name=name,
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


#: One object per Postgres type, shared by every column that uses it — the
#: convention `app/db/models.py` follows. These emit no DDL, so reuse is safe.
ERM_SUBJECT_KIND = _pg_enum(ErmSubjectKind, "erm_subject_kind")
ERM_SYNC_STATE = _pg_enum(ErmSyncState, "erm_sync_state")


class ErmSyncTask(Base):
    """`public.erm_sync_tasks` — one ERM job card (CLAUDE.md §10, migration 1900).

    ERM has no API and no scraper is permitted, so the integration is modelled as
    a task: the system generates the field pack, a named person pastes it, they
    confirm. This row is that task, and after confirmation it is the evidence
    that the paste happened — who, when, and the exact ordered values handed
    over.

    Three properties worth knowing before writing against it:

    * **`field_pack` is NULL until confirm.** An open card renders its pack live
      from the source record on every read, so it can never hand somebody last
      Tuesday's values. The column is stamped at confirm with what was actually
      pasted, and it is a JSON **array** because order is the deliverable.
    * **Nothing in Python sets `state` to STALE.** Drift is detected by the
      triggers in 1900, inside the transaction that does the editing, on every
      connection including the browser's direct PostgREST writes. See
      `app/services/erm/drift.py` for the long version.
    * **No commercial value is ever on this row.** Neither pack carries a rate, a
      bank rail or a P&L line — R5, and `tests/unit/test_erm_fieldpack.py`
      asserts it — because this table is readable by an LDE Executive on the same
      terms `trainers` is.
    """

    __tablename__ = "erm_sync_tasks"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True)

    #: `trainer` or `program` — the two ERM touchpoints the legacy folder
    #: evidences (workflow steps 13 and 8). Exactly one of the two FKs below is
    #: set, and 1900's CHECK forces it to agree with this label.
    subject_kind: Mapped[ErmSubjectKind] = mapped_column(ERM_SUBJECT_KIND, nullable=False)
    trainer_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("trainers.id", ondelete="CASCADE")
    )
    program_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("programs.id", ondelete="CASCADE")
    )

    state: Mapped[ErmSyncState] = mapped_column(ERM_SYNC_STATE, nullable=False)

    #: Which declared field order the pack used. ERM's real order is UNVERIFIED
    #: (`app/services/erm/fieldpack.py`); this column is what keeps that guess
    #: falsifiable once somebody writes the real one down.
    field_order_version: Mapped[int] = mapped_column(Integer, nullable=False)

    #: §10: "assigns it to a named person".
    assigned_to: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    assigned_by: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)

    #: The ordered pack as handed over, frozen at confirm. A JSON array.
    field_pack: Mapped[list[Any] | None] = mapped_column(JSONB)
    #: Source path -> rendered value behind that pack. What lets a screen name
    #: WHICH field drifted rather than only that something did.
    source_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: Read back off ERM's own screen after the paste — the legacy update logs'
    #: "ERM Trainer ID" / "ERM Program ID" column. Deliberately not a pack field.
    erm_external_id: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text)

    confirmed_by: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)

    #: Set by the 1900 triggers, never by this application.
    stale_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    stale_reason: Mapped[str | None] = mapped_column(Text)

    #: The confirmed card this one replaces after drift. §10's "and requeue".
    supersedes_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("erm_sync_tasks.id", ondelete="SET NULL")
    )

    cancelled_by: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ)
    cancelled_reason: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profiles.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
